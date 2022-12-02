# This code is based on https://github.com/openai/guided-diffusion
"""
Generate a large batch of image samples from a model and save them as a large
numpy array. This can be used to produce samples for FID evaluation.
"""
import json
import logging
import os

import numpy as np
import torch
from data_loaders.humanml.utils import paramUtil

from mdm.data_loaders.humanml.data.dataset import HumanML3D
from mdm.data_loaders.humanml.scripts import motion_process
from mdm.data_loaders.tensors import t2m_collate
from mdm.model import cfg_sampler
from mdm.utils import dist_util, fixseed, model_util
from motion_utils import DATA_PATH, NumpyJSONEncoder, dotdict

SMPL_DATA_PATH = os.path.join(DATA_PATH, 'body_models', 'smpl')
SMPL_MODEL_PATH = os.path.join(SMPL_DATA_PATH, 'SMPL_NEUTRAL.pkl')
JOINT_REGRESSOR_TRAIN_EXTRA_PATH = os.path.join(SMPL_DATA_PATH, 'J_regressor_extra.npy')
MODEL_PATH = os.path.join(DATA_PATH, 'save', 'humanml_trans_enc_512', 'model000200000.pt')
DATASET_PATH = os.path.join(DATA_PATH, 'dataset')
DATASET_OPT_PATH = os.path.join(DATASET_PATH, 'humanml_opt.txt')


def main():
    seed = 1
    max_frames = 196
    guidance_param = 2.5
    device = 0
    text_condition = "a person jumps"
    num_samples = 1
    batch_size = 1
    dataset = 'humanml'
    prefix_end = 0.25
    suffix_start = 0.75

    fixseed.fixseed(seed)

    dist_util.setup_dist(device)

    humanml3d = HumanML3D(mode='train', base_path=DATA_PATH, split='test', num_frames=max_frames)

    logging.info("Creating model and diffusion...")
    model = model_util.MDM(
        smpl_model_path=SMPL_MODEL_PATH,
        joint_regressor_train_extra_path=JOINT_REGRESSOR_TRAIN_EXTRA_PATH,
        **model_util.get_model_args(dotdict({
            'dataset': dataset,
            'latent_dim': 512,
            'layers': 8,
            'cond_mask_prob': 0.1,
            'arch': 'trans_enc',
            'emb_trans_dec': False,
        }), dotdict({'num_actions': 1, }))
    )

    diffusion = model_util.create_gaussian_diffusion(dotdict({
        'noise_schedule': 'cosine',
        'sigma_small': True,
        'lambda_vel': 0.0,
        'lambda_rcxyz': 0.0,
        'lambda_fc': 0.0,
    }))

    logging.info(f"Loading checkpoints from [{MODEL_PATH}]...")
    state_dict = torch.load(MODEL_PATH, map_location='cpu')
    model_util.load_model_wo_clip(model, state_dict)

    model = cfg_sampler.ClassifierFreeSampleModel(model)   # wrapping model with the classifier-free sampler
    model.to(dist_util.dev())
    model.eval()  # disable random masking

    input_motions, model_kwargs = t2m_collate([humanml3d[1]])
    input_motions = input_motions.to(dist_util.dev())

    if text_condition == '':
        guidance_param = 0.  # Force unconditioned generation

    lengths = model_kwargs['y']['lengths'].tolist()

    # add inpainting mask according to args
    assert max_frames == input_motions.shape[-1]
    inpainting_mask = torch.ones_like(
        input_motions,
        dtype=torch.bool,
        device=input_motions.device
    )  # True means use gt motion
    for i, length in enumerate(lengths):
        start_idx, end_idx = int(prefix_end * length), int(suffix_start * length)
        inpainting_mask[i, :, :, start_idx: end_idx] = False  # do inpainting in those frames

    logging.info('### Start sampling')

    new_func(input_motions, lengths, humanml3d.t2m_dataset, 'input_frames.json')

    sample = diffusion.p_sample_loop(
        model,
        (batch_size, model.njoints, model.nfeats, max_frames),
        clip_denoised=False,
        model_kwargs={'y': {
            'text': [text_condition] * num_samples,
            'inpainted_motion': input_motions,
            'inpainting_mask': inpainting_mask,
            'scale': torch.ones(batch_size, device=dist_util.dev()) * guidance_param,  # add CFG scale to batch
        }},
        skip_timesteps=0,  # 0 is the default value - i.e. don't skip any step
        init_image=None,
        progress=True,
        dump_steps=None,
        noise=None,
        const_noise=False,
    )

    new_func(sample, lengths, humanml3d.t2m_dataset, 'sample_frames.json')


def new_func(sample, lengths, t2m_dataset, output_json_path):
    model_data_rep = 'hml_vec'
    # Recover XYZ *positions* from HumanML3D vector representation
    if model_data_rep == 'hml_vec':
        n_joints = 22 if sample.shape[1] == 263 else 21
        sample = t2m_dataset.inv_transform(sample.cpu().permute(0, 2, 3, 1)).float()
        sample = motion_process.recover_from_ric(sample, n_joints)
        sample = sample.view(-1, *sample.shape[2:]).permute(0, 2, 3, 1)

    dataset = 'humanml'
    kinematic_tree = paramUtil.t2m_kinematic_chain
    chain_names = ['leg.r', 'leg.l', 'spine', 'arm.r', 'arm.l']

    output_motion: np.ndarray = sample.cpu().numpy()[0]
    output_length = lengths[0]

    logging.info("created samples")

    joints = output_motion.transpose(2, 0, 1)[:output_length]
    data = joints.copy().reshape(len(joints), -1, 3)

    frame_count = data.shape[0]

    chain_frames = [
        {
            chain_name: list(zip(
                data[frame, chain, 0],
                data[frame, chain, 1],
                data[frame, chain, 2]
            ))
            for chain_name, chain in zip(chain_names, kinematic_tree)
        }
        for frame in range(frame_count)
    ]

    with open(output_json_path, 'w', encoding='utf-8') as outfile:
        json.dump(chain_frames, outfile, indent=2, cls=NumpyJSONEncoder)


if __name__ == "__main__":
    main()
