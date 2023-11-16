import torch
import math
import numpy
import torch.nn.functional as F
import torch.nn as nn

from ..utils import correlation
from ..models.softsplat import softsplat


class SelfAttention(nn.Module):
    def __init__(self, embed_size, heads):
        super(SelfAttention, self).__init__()
        self.embed_size = embed_size
        self.heads = heads
        self.head_dim = embed_size // heads

        assert (
                self.head_dim * heads == embed_size
        ), "Embedding size needs to be divisible by heads"

        self.values = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.keys = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.queries = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.fc_out = nn.Linear(heads * self.head_dim, embed_size)

    def forward(self, prev_frame, next_frame):
        N = prev_frame.shape[0]
        value_len, key_len, query_len = prev_frame.shape[1], prev_frame.shape[1], next_frame.shape[1]

        values = prev_frame.reshape(N, value_len, self.heads, self.head_dim)
        keys = values
        query = next_frame.reshape(N, query_len, self.heads, self.head_dim)

        values = self.values(values)
        keys = self.keys(keys)
        query = self.queries(query)

        energy = torch.einsum("nqhd,nkhd->nhqk", [query, keys])
        attention = torch.softmax(energy / (self.embed_size ** (1 / 2)), dim=3)

        out = torch.einsum("nhql,nlhd->nqhd", [attention, values]).reshape(
            N, query_len, self.heads, self.head_dim
        )
        out_reshaped = out.view(N, query_len, -1)

        out_fc = self.fc_out(out_reshaped)

        out_fc_reshaped = out_fc.view(N, query_len, self.heads, self.head_dim)
        return out_fc_reshaped


# **************************************************************************************************#
# => Feature Pyramid
# **************************************************************************************************#
class FeatPyramid(nn.Module):
    """A 3-level feature pyramid, which by default is shared by the motion
    estimator and synthesis network.
    """

    def __init__(self):
        super(FeatPyramid, self).__init__()
        self.conv_stage0 = nn.Sequential(
            nn.Conv2d(in_channels=3, out_channels=16, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=16, out_channels=16, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=16, out_channels=16, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=16, out_channels=16, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))
        self.conv_stage1 = nn.Sequential(
            nn.Conv2d(in_channels=16, out_channels=32, kernel_size=3,
                      stride=2, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))
        self.conv_stage2 = nn.Sequential(
            nn.Conv2d(in_channels=32, out_channels=64, kernel_size=3,
                      stride=2, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1),
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3,
                      stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))

    def forward(self, img):
        C0 = self.conv_stage0(img)
        C1 = self.conv_stage1(C0)
        C2 = self.conv_stage2(C1)
        return [C0, C1, C2]


# **************************************************************************************************#
# => Motion Estimation
# **************************************************************************************************#
class MotionEstimator(nn.Module):
    """Bi-directional optical flow estimator
    1) construct partial cost volume with the CNN features from the stage 2 of
    the feature pyramid;
    2) estimate bi-directional flows, by feeding cost volume, CNN features for
    both warped images, CNN feature and estimated flow from previous iteration.
    """

    def __init__(self):
        super(MotionEstimator, self).__init__()
        # (4*2 + 1) ** 2 + 64 * 2 + 64 + 4 = 277
        self.conv_layer1 = nn.Sequential(
            nn.Conv2d(in_channels=277, out_channels=160,
                      kernel_size=1, stride=1, padding=0),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))
        self.conv_layer2 = nn.Sequential(
            nn.Conv2d(in_channels=160, out_channels=128,
                      kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))
        self.conv_layer3 = nn.Sequential(
            nn.Conv2d(in_channels=128, out_channels=112,
                      kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))
        self.conv_layer4 = nn.Sequential(
            nn.Conv2d(in_channels=112, out_channels=96,
                      kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))
        self.conv_layer5 = nn.Sequential(
            nn.Conv2d(in_channels=96, out_channels=64,
                      kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(inplace=False, negative_slope=0.1))
        self.conv_layer6 = nn.Sequential(
            nn.Conv2d(in_channels=64, out_channels=4,
                      kernel_size=3, stride=1, padding=1))

    def forward(self, feat0, feat1, last_feat, last_flow):
        corr_fn = correlation.FunctionCorrelation
        feat0 = softsplat.FunctionSoftsplat(
            tenInput=feat0, tenFlow=last_flow[:, :2] * 0.25 * 0.5,
            tenMetric=None, strType='average')
        feat1 = softsplat.FunctionSoftsplat(
            tenInput=feat1, tenFlow=last_flow[:, 2:] * 0.25 * 0.5,
            tenMetric=None, strType='average')

        volume = F.leaky_relu(
            input=corr_fn(tenFirst=feat0, tenSecond=feat1),
            negative_slope=0.1, inplace=False)
        input_feat = torch.cat([volume, feat0, feat1, last_feat, last_flow], 1)
        feat = self.conv_layer1(input_feat)
        feat = self.conv_layer2(feat)
        feat = self.conv_layer3(feat)
        feat = self.conv_layer4(feat)
        feat = self.conv_layer5(feat)
        flow = self.conv_layer6(feat)

        return flow, feat


# **************************************************************************************************#
# => Frame Synthesis
# **************************************************************************************************#
class SynthesisNetwork(nn.Module):
    def __init__(self):
        super(SynthesisNetwork, self).__init__()
        # Innovating based on UPR-Net
        self.self_attention = SelfAttention(embed_size=64, heads=4)
        input_channels = 9 + 4 + 6
        self.encoder_conv = nn.Sequential(
            nn.Conv2d(in_channels=input_channels, out_channels=32,
                      kernel_size=3, stride=1, padding=1),
            nn.PReLU(num_parameters=32),
            nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=32))
        self.encoder_down1 = nn.Sequential(
            nn.Conv2d(in_channels=32 + 16 + 16, out_channels=64,
                      kernel_size=3, stride=2, padding=1),
            nn.PReLU(num_parameters=64),
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=64),
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=64))
        self.encoder_down2 = nn.Sequential(
            nn.Conv2d(in_channels=64 + 32 + 32, out_channels=128,
                      kernel_size=3, stride=2, padding=1),
            nn.PReLU(num_parameters=128),
            nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=128),
            nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=128))
        self.decoder_up1 = nn.Sequential(
            torch.nn.ConvTranspose2d(in_channels=128 + 64 + 64,
                                     out_channels=64, kernel_size=4, stride=2,
                                     padding=1, bias=True),
            nn.PReLU(num_parameters=64),
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=64))
        self.decoder_up2 = nn.Sequential(
            torch.nn.ConvTranspose2d(in_channels=64 + 64,
                                     out_channels=32, kernel_size=4, stride=2,
                                     padding=1, bias=True),
            nn.PReLU(num_parameters=32),
            nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=32))
        self.decoder_conv = nn.Sequential(
            nn.Conv2d(in_channels=32 + 32, out_channels=32, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=32),
            nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3,
                      stride=1, padding=1),
            nn.PReLU(num_parameters=32))
        self.pred = nn.Conv2d(in_channels=32, out_channels=5, kernel_size=3,
                              stride=1, padding=1)

    def get_warped_representations(self, bi_flow, c0, c1,
                                   i0=None, i1=None, time_period=0.5):
        flow_0t = bi_flow[:, :2] * time_period
        flow_1t = bi_flow[:, 2:4] * (1 - time_period)
        warped_c0 = softsplat.FunctionSoftsplat(
            tenInput=c0, tenFlow=flow_0t,
            tenMetric=None, strType='average')
        warped_c1 = softsplat.FunctionSoftsplat(
            tenInput=c1, tenFlow=flow_1t,
            tenMetric=None, strType='average')
        if (i0 is None) and (i1 is None):
            return warped_c0, warped_c1
        else:
            warped_img0 = softsplat.FunctionSoftsplat(
                tenInput=i0, tenFlow=flow_0t,
                tenMetric=None, strType='average')
            warped_img1 = softsplat.FunctionSoftsplat(
                tenInput=i1, tenFlow=flow_1t,
                tenMetric=None, strType='average')
            flow_0t_1t = torch.cat((flow_0t, flow_1t), 1)
            return warped_img0, warped_img1, warped_c0, warped_c1, flow_0t_1t

    def forward(self, last_i_rgb, i0_rgb, i1_rgb, last_i_depth, i0_depth, i1_depth, c0_pyr_rgb, c1_pyr_rgb,
                c0_pyr_depth, c1_pyr_depth, bi_flow_pyr,
                time_period=0.5, cross_att=None):
        warped_img0_rgb, warped_img0_depth, warped_img1_depth, warped_img1_rgb, warped_c0, warped_c1, flow_0t_1t_rgb, flow_0t_1t_depth = \
            self.get_warped_representations(
                bi_flow_pyr[0], c0_pyr_rgb[0], c1_pyr_rgb[0], i0_rgb, i1_rgb, c0_pyr_depth[0], c1_pyr_depth[0],
                i0_depth, i1_depth,
                time_period=time_period)

        # Adapted for multimodal fusion
        input_feat_rgb = torch.cat((last_i_rgb, warped_img0_rgb, warped_img1_rgb, i0_rgb, i1_rgb, flow_0t_1t_rgb), 1)
        input_feat_depth = torch.cat(
            (last_i_depth, warped_img0_depth, warped_img1_depth, i0_depth, i1_depth, flow_0t_1t_depth), 1)
        input_feat = torch.cat((input_feat_rgb, input_feat_depth), 1)

        s0 = self.encoder_conv(input_feat)
        s1 = self.encoder_down1(torch.cat((s0, warped_c0, warped_c1), 1))

        warped_c0, warped_c1, _ = self.get_warped_representations(
            bi_flow_pyr[1], c0_pyr_rgb[1], c1_pyr_rgb[1], c0_pyr_depth[1], c1_pyr_depth[1], time_period=time_period)
        s2 = self.encoder_down2(torch.cat((s1, warped_c0, warped_c1), 1))

        # Add attention mechanism for feature fusion
        cross_att = self.self_attention(cross_att, cross_att)

        x = self.decoder_up1(torch.cat((s2, warped_c0, warped_c1), 1))
        x = self.decoder_up2(torch.cat((cross_att, s1), 1))

        x = self.decoder_conv(torch.cat((x, s0), 1))

        # prediction
        refine = self.pred(x)
        refine_res = torch.sigmoid(refine[:, :3]) * 2 - 1
        refine_mask0 = torch.sigmoid(refine[:, 3:4])
        refine_mask1 = torch.sigmoid(refine[:, 4:5])
        merged_img = (warped_img0_rgb * refine_mask0 * (1 - time_period) + \
                      warped_img1_rgb * refine_mask1 * time_period)
        merged_img = merged_img / (refine_mask0 * (1 - time_period) + \
                                   refine_mask1 * time_period)
        interp_img = merged_img + refine_res
        interp_img = torch.clamp(interp_img, 0, 1)

        extra_dict = {}
        extra_dict["refine_res"] = refine_res
        extra_dict["warped_img0"] = warped_img0_rgb
        extra_dict["warped_img1"] = warped_img1_rgb
        extra_dict["merged_img"] = merged_img

        return interp_img, extra_dict


# **************************************************************************************************#
# => Unified model
# **************************************************************************************************#
class Model(nn.Module):
    def __init__(self, pyr_level=3, nr_lvl_skipped=0):
        super(Model, self).__init__()
        self.pyr_level = pyr_level
        self.nr_lvl_skipped = nr_lvl_skipped
        # Enable UPR-Net to process multi-modal data and achieve adaptive frame
        self.feat_pyramid_rgb = FeatPyramid()
        self.feat_pyramid_depth = FeatPyramid()
        self.motion_estimator = MotionEstimator()
        self.synthesis_network = SynthesisNetwork()

    def forward_one_lvl(self,
                        img0_rgb, img1_rgb, img0_depth, img1_depth, last_feat, last_flow, last_interp=None,
                        time_period=0.5, skip_me=False):

        # context feature extraction for RGB images
        feat0_pyr_rgb = self.feat_pyramid_rgb(img0_rgb)
        feat1_pyr_rgb = self.feat_pyramid_rgb(img1_rgb)

        # context feature extraction for depth images
        feat0_pyr_depth = self.feat_pyramid_depth(img0_depth)
        feat1_pyr_depth = self.feat_pyramid_depth(img1_depth)

        # concatenated RGB and depth features
        feat0_pyr = [torch.cat([rgb_feat, depth_feat], dim=1) for rgb_feat, depth_feat in
                     zip(feat0_pyr_rgb, feat0_pyr_depth)]
        feat1_pyr = [torch.cat([rgb_feat, depth_feat], dim=1) for rgb_feat, depth_feat in
                     zip(feat1_pyr_rgb, feat1_pyr_depth)]

        # bi-directional flow estimation
        if not skip_me:
            flow, feat = self.motion_estimator(
                feat0_pyr[-1], feat1_pyr[-1],
                last_feat, last_flow)
        else:
            flow = last_flow
            feat = last_feat

        # frame synthesis
        # optical flow is estimated at 1/4 resolution
        ori_resolution_flow = F.interpolate(
            input=flow, scale_factor=4.0,
            mode="bilinear", align_corners=False)

        ## construct 3-level flow pyramid for synthesis network
        bi_flow_pyr = []
        tmp_flow = ori_resolution_flow
        bi_flow_pyr.append(tmp_flow)
        for i in range(2):
            tmp_flow = F.interpolate(
                input=tmp_flow, scale_factor=0.5,
                mode="bilinear", align_corners=False) * 0.5
            bi_flow_pyr.append(tmp_flow)

        ## merge warped frames as initial interpolation for frame synthesis
        if last_interp is None:
            flow_0t = ori_resolution_flow[:, :2] * time_period
            flow_1t = ori_resolution_flow[:, 2:4] * (1 - time_period)
            warped_img0 = softsplat.FunctionSoftsplat(
                tenInput=img0_rgb, tenFlow=flow_0t,
                tenMetric=None, strType='average')
            warped_img1 = softsplat.FunctionSoftsplat(
                tenInput=img1_rgb, tenFlow=flow_1t,
                tenMetric=None, strType='average')
            last_interp = warped_img0 * (1 - time_period) \
                          + warped_img1 * time_period

        ## do synthesis
        interp_img, extra_dict = self.synthesis_network(
            last_interp, img0_rgb, img1_rgb, feat0_pyr, feat1_pyr, bi_flow_pyr,
            time_period=time_period)

        return flow, feat, interp_img, extra_dict

    def forward(self, img0, img1, time_period,
                pyr_level=None, nr_lvl_skipped=None):

        if pyr_level is None: pyr_level = self.pyr_level
        if nr_lvl_skipped is None: nr_lvl_skipped = self.nr_lvl_skipped
        N, _, H, W = img0.shape
        bi_flows = []
        interp_imgs = []
        skipped_levels = [] if nr_lvl_skipped == 0 else \
            list(range(pyr_level))[::-1][-nr_lvl_skipped:]

        # The original input resolution corresponds to level 0.
        for level in list(range(pyr_level))[::-1]:
            if level != 0:
                scale_factor = 1 / 2 ** level
                img0_this_lvl = F.interpolate(
                    input=img0, scale_factor=scale_factor,
                    mode="bilinear", align_corners=False)
                img1_this_lvl = F.interpolate(
                    input=img1, scale_factor=scale_factor,
                    mode="bilinear", align_corners=False)
            else:
                img0_this_lvl = img0
                img1_this_lvl = img1

            # skip motion estimation, directly use up-sampled optical flow
            skip_me = False

            # the lowest-resolution pyramid level
            if level == pyr_level - 1:
                last_flow = torch.zeros(
                    (N, 4, H // (2 ** (level + 2)), W // (2 ** (level + 2)))
                ).to(img0.device)
                last_feat = torch.zeros(
                    (N, 64, H // (2 ** (level + 2)), W // (2 ** (level + 2)))
                ).to(img0.device)
                last_interp = None
            # skip some levels for both motion estimation and frame synthesis
            elif level in skipped_levels[:-1]:
                continue
            # last level (original input resolution), only skip motion estimation
            elif (level == 0) and len(skipped_levels) > 0:
                if len(skipped_levels) == pyr_level:
                    last_flow = torch.zeros(
                        (N, 4, H // 4, W // 4)).to(img0.device)
                    last_interp = None
                else:
                    resize_factor = 2 ** len(skipped_levels)
                    last_flow = F.interpolate(
                        input=flow, scale_factor=resize_factor,
                        mode="bilinear", align_corners=False) * resize_factor
                    last_interp = F.interpolate(
                        input=interp_img, scale_factor=resize_factor,
                        mode="bilinear", align_corners=False)
                skip_me = True
            # last level (original input resolution), motion estimation + frame
            # synthesis
            else:
                last_flow = F.interpolate(input=flow, scale_factor=2.0,
                                          mode="bilinear", align_corners=False) * 2
                last_feat = F.interpolate(input=feat, scale_factor=2.0,
                                          mode="bilinear", align_corners=False) * 2
                last_interp = F.interpolate(
                    input=interp_img, scale_factor=2.0,
                    mode="bilinear", align_corners=False)

            flow, feat, interp_img, _ = self.forward_one_lvl(
                img0_this_lvl, img1_this_lvl,
                last_feat, last_flow, last_interp,
                time_period, skip_me=skip_me)
            bi_flows.append(
                F.interpolate(input=flow, scale_factor=4.0,
                              mode="bilinear", align_corners=False))

        # directly up-sample estimated flow to full resolution with bi-linear
        # interpolation
        bi_flow = F.interpolate(
            input=flow, scale_factor=4.0,
            mode="bilinear", align_corners=False)

        interp_imgs.append(interp_img)

        return img0, interp_img, bi_flow, \
            {"interp_imgs": interp_imgs, "bi_flows": bi_flows}


if __name__ == "__main__":
    pass
