import torch
import torch.nn as nn
import torch.nn.functional as F
from timm.models.swin_transformer import SwinTransformerStage


class ThoughtAttention(nn.Module):
    def __init__(self, dim_q, dim_kv, num_heads=8):
        super(ThoughtAttention, self).__init__()
        self.num_heads = num_heads
        self.dim_q = dim_q
        self.dim_kv = dim_kv
        self.dim_head = dim_q // num_heads

        assert dim_q % num_heads == 0, "dim_q must be divisible by num_heads"

        self.W_Q = nn.Linear(dim_q, dim_q, bias=False)
        self.W_K = nn.Linear(dim_kv, dim_q, bias=False)
        self.W_V = nn.Linear(dim_kv, dim_q, bias=False)
        self.W_O = nn.Linear(dim_q, dim_q, bias=False)

        self.scale = self.dim_head ** -0.5

        # Maximum relative position for dynamic computation
        self.max_relative_position = 20  # Adjust based on expected maximum spatial dimension

        # Relative position bias table
        num_relative_positions = (2 * self.max_relative_position - 1) ** 2
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros(num_relative_positions, num_heads)
        )
        nn.init.trunc_normal_(self.relative_position_bias_table, std=0.02)

    def get_relative_position_index(self, H, W):
        coords_h = torch.arange(H)
        coords_w = torch.arange(W)
        coords = torch.stack(torch.meshgrid(coords_h, coords_w))  # 2, H, W
        coords_flatten = torch.flatten(coords, 1)  # 2, N
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, N, N
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # N, N, 2

        # Shift to start from 0
        relative_coords[:, :, 0] += self.max_relative_position - 1
        relative_coords[:, :, 1] += self.max_relative_position - 1

        relative_coords[:, :, 0] *= 2 * self.max_relative_position - 1
        relative_position_index = relative_coords.sum(-1)  # N, N

        return relative_position_index

    def forward(self, query, key, value, H, W):
        """
        query: (B, N_q, dim_q)
        key: (B, N_kv, dim_kv)
        value: (B, N_kv, dim_kv)
        H, W: Spatial dimensions of the feature map
        """
        B, N_q, _ = query.size()
        _, N_kv, _ = key.size()
        assert N_q == N_kv, "The number of query and key tokens must be equal."

        # Linear projections
        Q = self.W_Q(query).view(B, N_q, self.num_heads, self.dim_head).transpose(1, 2)  # (B, num_heads, N_q, dim_head)
        K = self.W_K(key).view(B, N_kv, self.num_heads, self.dim_head).transpose(1, 2)   # (B, num_heads, N_kv, dim_head)
        V = self.W_V(value).view(B, N_kv, self.num_heads, self.dim_head).transpose(1, 2) # (B, num_heads, N_kv, dim_head)

        # Scaled dot-product attention
        attn = (Q @ K.transpose(-2, -1)) * self.scale  # (B, num_heads, N_q, N_kv)

        # Compute relative position index dynamically
        relative_position_index = self.get_relative_position_index(H, W)  # (N_q, N_kv)
        relative_position_index = relative_position_index.to(query.device)

        # Get relative position biases
        relative_position_bias = self.relative_position_bias_table[relative_position_index.view(-1)].view(
            N_q, N_kv, -1
        )  # N_q, N_kv, num_heads
        relative_position_bias = relative_position_bias.permute(2, 0, 1).unsqueeze(0)  # 1, num_heads, N_q, N_kv

        # Add relative position bias to attention
        attn = attn + relative_position_bias

        attn = nn.functional.softmax(attn, dim=-1)

        # Attention output
        out = (attn @ V).transpose(1, 2).reshape(B, N_q, self.dim_q)
        out = self.W_O(out)

        # Residual connection and layer normalization
        out = out + query
        out = nn.LayerNorm(self.dim_q).to(query.device)(out)

        return out


class ThoughtGenerator(nn.Module):
    def __init__(self, config):
        super(ThoughtGenerator, self).__init__()
        self.num_stages = config.get('num_stages', 4)
        self.in_channels_list = config['in_channels_list']
        self.num_heads = config['num_heads']

        # Initialize fusion modules (ThoughtAttention)
        self.fusions = nn.ModuleList()
        for i in range(self.num_stages - 1):
            fusion = ThoughtAttention(
                dim_q=self.in_channels_list[i],
                dim_kv=self.in_channels_list[i+1],
                num_heads=self.num_heads[i]
            )
            self.fusions.append(fusion)

    def forward(self, features):
        """
        features: List of feature maps from the backbone [F_0, F_1, F_2, F_3]
        """
        fused_features = [features[-1]]  # Start with the last stage's feature F_3
        # Perform fusion from back to front
        for i in range(self.num_stages - 2, -1, -1):
            # Get current feature and next fused feature
            F_current = features[i]
            F_next = fused_features[0]

            # Ensure spatial dimensions match
            B, C_current, H_current, W_current = F_current.size()
            B, C_next, H_next, W_next = F_next.size()
            if H_current != H_next or W_current != W_next:
                F_next = nn.functional.interpolate(F_next, size=(H_current, W_current), mode='bilinear', align_corners=False)

            # Flatten spatial dimensions for attention
            F_current_flat = F_current.view(B, C_current, H_current * W_current).permute(0, 2, 1)  # (B, N_current, C_current)
            F_next_flat = F_next.view(B, C_next, H_current * W_current).permute(0, 2, 1)  # (B, N_current, C_next)

            # Apply ImprovedCrossAttention
            F_fused_flat = self.fusions[i](F_current_flat, F_next_flat, F_next_flat, H_current, W_current)  # (B, N_current, C_current)

            # Reshape back to spatial dimensions
            F_fused = F_fused_flat.permute(0, 2, 1).view(B, C_current, H_current, W_current)

            # Prepend the fused feature to the list
            fused_features.insert(0, F_fused)  # Now fused_features[0] is F_i'

        # Generate classification outputs
        logits_list = []
        for i in range(self.num_stages):
            F_i = fused_features[i]  # Fused feature at stage i
            B, C_i, H_i, W_i = F_i.size()

            # Global average pooling
            F_i_pooled = F_i.mean(dim=[2, 3])  # (B, C_i)

            # Compute logits
            logits_i = self.classifiers[i](F_i_pooled)  # (B, num_classes_i)

            logits_list.append(logits_i)

        return fused_features, logits_list
