import torch
import torch.nn as nn
from functools import partial

from timm.models.layers import DropPath, to_2tuple
import math


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.dwconv = DWConv(hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x, H, W):
        x = self.fc1(x)
        x = self.act(x + self.dwconv(x, H, W))
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, ca_num_heads=4, sa_num_heads=8, qkv_bias=False, qk_scale=None, 
                       attn_drop=0., proj_drop=0., ca_attention=1, expand_ratio=2):
        super().__init__()

        self.ca_attention = ca_attention
        self.dim = dim
        self.ca_num_heads = ca_num_heads
        self.sa_num_heads = sa_num_heads

        assert dim % ca_num_heads == 0, f"dim {dim} should be divided by num_heads {ca_num_heads}."
        assert dim % sa_num_heads == 0, f"dim {dim} should be divided by num_heads {sa_num_heads}."

        self.act = nn.GELU()
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.split_groups=self.dim//ca_num_heads

        if ca_attention == 1:
            self.v = nn.Linear(dim, dim, bias=qkv_bias)
            self.s = nn.Linear(dim, dim, bias=qkv_bias)
            for i in range(self.ca_num_heads):
                local_conv = nn.Conv2d(dim//self.ca_num_heads, dim//self.ca_num_heads, kernel_size=(3+i*2), padding=(1+i), stride=1, groups=dim//self.ca_num_heads)
                setattr(self, f"local_conv_{i + 1}", local_conv)
            self.proj0 = nn.Conv2d(dim, dim*expand_ratio, kernel_size=1, padding=0, stride=1, groups=self.split_groups)
            self.bn = nn.BatchNorm2d(dim*expand_ratio)
            self.proj1 = nn.Conv2d(dim*expand_ratio, dim, kernel_size=1, padding=0, stride=1)

        else:
            head_dim = dim // sa_num_heads
            self.scale = qk_scale or head_dim ** -0.5
            self.q = nn.Linear(dim, dim, bias=qkv_bias)
            self.attn_drop = nn.Dropout(attn_drop)
            self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
            self.local_conv = nn.Conv2d(dim, dim, kernel_size=3, padding=1, stride=1, groups=dim)

    def forward(self, x, H, W):
        B, N, C = x.shape
        if self.ca_attention == 1:
            v = self.v(x)
            s = self.s(x).reshape(B, H, W, self.ca_num_heads, C//self.ca_num_heads).permute(3, 0, 4, 1, 2)
            for i in range(self.ca_num_heads):
                local_conv = getattr(self, f"local_conv_{i + 1}")
                s_i= s[i]
                s_i = local_conv(s_i).reshape(B, self.split_groups, -1, H, W)
                if i == 0:
                    s_out = s_i
                else:
                    s_out = torch.cat([s_out,s_i],2)
            s_out = s_out.reshape(B, C, H, W)
            s_out = self.proj1(self.act(self.bn(self.proj0(s_out))))
            self.modulator = s_out
            s_out = s_out.reshape(B, C, N).permute(0, 2, 1)
            x = s_out * v

        else:
            q = self.q(x).reshape(B, N, self.sa_num_heads, C // self.sa_num_heads).permute(0, 2, 1, 3)
            kv = self.kv(x).reshape(B, -1, 2, self.sa_num_heads, C // self.sa_num_heads).permute(2, 0, 3, 1, 4)
            k, v = kv[0], kv[1]
            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            attn = self.attn_drop(attn)
            x = (attn @ v).transpose(1, 2).reshape(B, N, C) + \
                self.local_conv(v.transpose(1, 2).reshape(B, N, C).transpose(1, 2).view(B,C, H, W)).view(B, C, N).transpose(1, 2)

        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class Block(nn.Module):

    def __init__(self, dim, ca_num_heads, sa_num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None,
                    use_layerscale=False, layerscale_value=1e-4, drop=0., attn_drop=0.,
                    drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, ca_attention=1,expand_ratio=2):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim,
            ca_num_heads=ca_num_heads, sa_num_heads=sa_num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, ca_attention=ca_attention, 
            expand_ratio=expand_ratio)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        self.gamma_1 = 1.0
        self.gamma_2 = 1.0
        if use_layerscale:
            self.gamma_1 = nn.Parameter(layerscale_value * torch.ones((dim)), requires_grad=True)
            self.gamma_2 = nn.Parameter(layerscale_value * torch.ones((dim)), requires_grad=True)

    def forward(self, x, H, W):

        x = x + self.drop_path(self.gamma_1 * self.attn(self.norm1(x), H, W))
        x = x + self.drop_path(self.gamma_2 * self.mlp(self.norm2(x), H, W))

        return x


class OverlapPatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """
    def __init__(self, img_size=224, patch_size=3, stride=2, in_chans=3, embed_dim=768):
        super().__init__()
        patch_size = to_2tuple(patch_size)
        img_size = to_2tuple(img_size)

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride,
                              padding=(patch_size[0] // 2, patch_size[1] // 2))
        self.norm = nn.LayerNorm(embed_dim)

    def forward(self, x):
        x = self.proj(x)
        _, _, H, W = x.shape
        x = x.flatten(2).transpose(1, 2)
        x = self.norm(x)

        return x, H, W


class Head(nn.Module):
    def __init__(self, head_conv, dim):
        super(Head, self).__init__()
        stem = [nn.Conv2d(3, dim, head_conv, 2, padding=3 if head_conv==7 else 1, bias=False), nn.BatchNorm2d(dim), nn.ReLU(True)]
        stem.append(nn.Conv2d(dim, dim, kernel_size=2, stride=2))
        self.conv = nn.Sequential(*stem)
        self.norm = nn.LayerNorm(dim)

    def forward(self, x):
        x = self.conv(x)
        _, _, H, W = x.shape
        x = x.flatten(2).transpose(1, 2)
        x = self.norm(x)
        return x, H, W


class DWConv(nn.Module):
    def __init__(self, dim=768):
        super(DWConv, self).__init__()
        self.dwconv = nn.Conv2d(dim, dim, 3, 1, 1, bias=True, groups=dim)

    def forward(self, x, H, W):
        B, N, C = x.shape
        x = x.transpose(1, 2).view(B, C, H, W)
        x = self.dwconv(x)
        x = x.flatten(2).transpose(1, 2)

        return x


class SMT(nn.Module):
    def __init__(self, config, img_size=224):
        super().__init__()
        self.num_stages = 4

        img_size = img_size
        head_conv = config.IN_CHANS
        embed_dims = config.EMBED_DIMS
        depths = config.DEPTHS
        ca_num_heads = config.CA_NUM_HEADS
        sa_num_heads = config.SA_NUM_HEADS
        mlp_ratios = config.MLP_RATIOS
        qkv_bias = config.QKV_BIAS
        qk_scale = config.QK_SCALE
        use_layerscale = config.USE_LAYERSCALE
        layerscale_value = config.LAYERSCALE_VALUE
        proj_drop_rate = config.PROJ_DROP_RATE
        attn_drop_rate = config.ATTN_DROP_RATE
        drop_path_rate = config.DROP_PATH_RATE
        ca_attentions = config.CA_ATTENTIONS
        expand_ratio = config.EXPAND_RATIO
        norm_layer = partial(nn.LayerNorm, eps=1e-6)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule
        cur = 0

        for i in range(self.num_stages):
            if i == 0:
                patch_embed = Head(head_conv, embed_dims[i])
            else:
                patch_embed = OverlapPatchEmbed(img_size=img_size if i == 0 else img_size // (2 ** (i + 1)),
                                            patch_size=3,
                                            stride=2,
                                            in_chans=embed_dims[i - 1],
                                            embed_dim=embed_dims[i])

            block = nn.ModuleList([Block(
                dim=embed_dims[i], ca_num_heads=ca_num_heads[i], sa_num_heads=sa_num_heads[i], mlp_ratio=mlp_ratios[i], qkv_bias=qkv_bias, qk_scale=qk_scale,
                use_layerscale=use_layerscale, 
                layerscale_value=layerscale_value,
                drop=proj_drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + j], norm_layer=norm_layer,
                ca_attention=0 if i==2 and j%2!=0 else ca_attentions[i], expand_ratio=expand_ratio)
                for j in range(depths[i])])
            norm = norm_layer(embed_dims[i])
            cur += depths[i]

            setattr(self, f"patch_embed{i + 1}", patch_embed)
            setattr(self, f"block{i + 1}", block)
            setattr(self, f"norm{i + 1}", norm)

    def forward_mero(self, x):
        features = []

        B = x.shape[0]

        for i in range(self.num_stages - 1):
            patch_embed = getattr(self, f"patch_embed{i + 1}")
            block = getattr(self, f"block{i + 1}")
            norm = getattr(self, f"norm{i + 1}")
            x, H, W = patch_embed(x)
            for blk in block:
                x = blk(x, H, W)
            x = norm(x)
            x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
            features.append(x)

        return features

    def forward_base(self, x):
        B, N, _ = x.shape
        H = W = int(math.sqrt(N))
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()

        patch_embed = getattr(self, f"patch_embed{self.num_stages}")
        block = getattr(self, f"block{self.num_stages}")
        norm = getattr(self, f"norm{self.num_stages}")
        x, H, W = patch_embed(x)
        for blk in block:
            x = blk(x, H, W)
        x = norm(x)

        return x

    def forward(self, x):
        features = []

        B = x.shape[0]

        for i in range(self.num_stages):
            patch_embed = getattr(self, f"patch_embed{i + 1}")
            block = getattr(self, f"block{i + 1}")
            norm = getattr(self, f"norm{i + 1}")
            x, H, W = patch_embed(x)
            for blk in block:
                x = blk(x, H, W)
            x = norm(x)
            if i != self.num_stages - 1:
                x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()

            features.append(x)

        return features
