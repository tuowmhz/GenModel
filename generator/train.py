from geomdl.exchange import import_txt
from pytorch_lightning import Trainer as LightningTrainer
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.callbacks import (
    LearningRateMonitor,
    ModelCheckpoint,
    RichProgressBar,
)
import numpy as np
import os
import sys
import cv2
from os.path import join as pjoin

BASEPATH = os.path.dirname(__file__)
sys.path.insert(0, BASEPATH)
sys.path.insert(0, pjoin(BASEPATH, '..'))
from diffusers.schedulers.scheduling_ddim import DDIMScheduler

from generator.diffusion import Diffusion
from generator.diffusion_utils import ConditionalUnet1D
from generator.dataloader import GripperDataset
from dynamics.parser import parse
from assets.finger_3d import generate_3d_ctrlpts
from assets.icon_process import extract_contours
from assets.scan_object_process import read_object_names
from dynamics.utils import sample_pts_from_mesh
from dynamics.profile_forward_3d import ProfileForward3DModel
from dynamics.profile_forward_2d import ProfileForward2DModel

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

torch.multiprocessing.set_sharing_strategy("file_system")

rank_idx = os.environ.get("NODE_RANK", 0)
# OBJECT_IDS = [10000, 2009, 2114, 2082, 1041, 2048, 1045, 1019]
# OBJECT_IDS = [0, 37, 50]
OBJECT_IDS = [0]


def train(args):
    total_num = args.num_fingers
    batch_size = min(args.batch_size, args.num_fingers)
    train_ids = list(range(int(total_num * 0.9))) # why it need to multiply 0。9
    val_ids = list(range(int(total_num * 0.9), total_num))
    gripper_pts = []
    for idx in range(total_num):
        rs = np.random.RandomState(idx)
        if args.fingers_3d:
            yl = rs.uniform(-0.1, 0, size=(21))
            yr = rs.uniform(-0.1, 0, size=(21))
            ctrlpts = generate_3d_ctrlpts(yl, yr)
            gripper_pts.append(ctrlpts)
        else:
            x = np.linspace(-0.12, 0.12, 7)
            yl = rs.uniform(-0.045, 0.015, size=(7))
            yr = rs.uniform(-0.045, 0.015, size=(7))
            ctrlptsl = np.stack([x, yl], axis=-1)
            ctrlptsr = np.stack([x, yr], axis=-1)
            ctrlpts = np.concatenate((ctrlptsl, ctrlptsr), axis=0)
            gripper_pts.append(ctrlpts)
    gripper_pts = np.stack(gripper_pts, axis=0)
    gripper_pts_max_x = 0.12
    gripper_pts_min_x = -0.12
    if args.fingers_3d:
        gripper_pts_max_y = 0
        gripper_pts_min_y = -0.1
    else:
        gripper_pts_max_y = 0.015
        gripper_pts_min_y = -0.045
    if args.mode == 'test':
        test_dataset = GripperDataset(gripper_pts, gripper_pts_max_x, gripper_pts_min_x, gripper_pts_max_y,
                                      gripper_pts_min_y)
        print('test dataset size:', len(test_dataset))
        test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False, num_workers=args.num_workers,
                                 drop_last=True)
    else:
        train_dataset = GripperDataset(gripper_pts[train_ids, ...], gripper_pts_max_x, gripper_pts_min_x,
                                       gripper_pts_max_y, gripper_pts_min_y)
        val_dataset = GripperDataset(gripper_pts[val_ids, ...], gripper_pts_max_x, gripper_pts_min_x, gripper_pts_max_y,
                                     gripper_pts_min_y)
        train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=args.num_workers,
                                  drop_last=False)
        val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, num_workers=args.num_workers,
                                drop_last=False)

    input_spline_dim = 1
    num_spline_points = args.ctrlpts_dim  # for 2d, ctrlpts_dim=14, for 3d, ctrlpts_dim=42
    pts_x_dim = args.ctrlpts_x_dim
    pts_z_dim = args.ctrlpts_z_dim
    unet = ConditionalUnet1D(input_dim=input_spline_dim, global_cond_dim=0, down_dims=[128, 256],
                             diffusion_step_embed_dim=32)
    mode = 'point_3d' if args.fingers_3d else 'point'
    input_dim = input_spline_dim
    scheduler = DDIMScheduler(num_train_timesteps=args.num_train_timesteps, beta_schedule='squaredcos_cap_v2',
                              clip_sample=True, prediction_type='epsilon')  # squared cosine beta schedule # DDIM schedule is a duffision model classifier - Denoising Diffusion Implicit models - improved upon the original Denoising Diffusion Probabilistic Model
    
    # In Line 89 - 97, it initialize the diffusion model process.
    
    if args.classifier_guidance:
        if args.fingers_3d:
            classifier_model = nn.DataParallel(ProfileForward3DModel(output_ch=3, params_ch=num_spline_points).cuda())
        else:
            classifier_model = nn.DataParallel(ProfileForward2DModel(output_ch=3, params_ch=num_spline_points,
                                                                     object_ch=2 * args.object_max_num_vertices).cuda())
            
    # In this section, it loaded the already trained classifier model from the bash command and initualize the parallel computing process - distribute the training batch to  multiple GPUs.
        print('loading classifier checkpoint from', args.checkpoint_path)
        classifier_model.load_state_dict(torch.load(args.checkpoint_path))
        for param in classifier_model.parameters():
            param.requires_grad = False
        if args.fingers_3d:
            object_pts_max_x = 0.1
            object_pts_min_x = -0.1
            object_pts_max_y = 0.1
            object_pts_min_y = -0.1
            object_pts_max_z = 0.12
            object_pts_min_z = 0.0
            object_ids = read_object_names(test=True)
            object_vertices = []
            for object_name in object_ids:
                mesh_file = os.path.join(args.object_dir, object_name, 'model.obj')
                pts = sample_pts_from_mesh(mesh_file, args.object_max_num_vertices)
                object_vertices.append(torch.from_numpy(pts).float())
            object_vertices = torch.stack(object_vertices, dim=0)
            object_vertices[..., 0] = (object_vertices[..., 0] - object_pts_min_x) / (
                
                        object_pts_max_x - object_pts_min_x) * 2.0 - 1.0
            object_vertices[..., 1] = (object_vertices[..., 1] - object_pts_min_y) / (
                        object_pts_max_y - object_pts_min_y) * 2.0 - 1.0
            object_vertices[..., 2] = (object_vertices[..., 2] - object_pts_min_z) / (
                        object_pts_max_z - object_pts_min_z) * 2.0 - 1.0
        else:
            object_pts_max_x = 0.05
            object_pts_min_x = -0.05
            object_pts_max_y = 0.05
            object_pts_min_y = -0.05
            object_vertices = []
            print("simple test")

            object_image = np.load(args.object_dir, allow_pickle=True).item()['image']

            # Load the object from the corresponding directionary.


            # # Load the .npy file
            # loaded_data = np.load(args.object_dir, allow_pickle=True).item()
            #
            # # Check for the 'image' key and handle missing key gracefully
            # if 'image' in loaded_data:
            #     object_image = loaded_data['image']
            #     print("Image data loaded successfully.")
            # else:
            #     raise KeyError(
            #         f"'image' key not found in the loaded file at {args.object_dir}. Available keys: {loaded_data.keys()}")

            print("Image Shape")
            print(len(object_image))
            print("Image Shape")
            print(object_image.shape)
            # print(object_image.shape)
            # print("Image Array")
            # print(object_image)
            object_ids = OBJECT_IDS
            #print("Image shape before cvtColor:", object_image.shape)
            for object_idx in object_ids:
                single_image = object_image[object_idx]
                print("Shape of single_image:", single_image.shape)  # Debugging info
                if len(single_image.shape) == 3:  # Check if the image has multiple channels
                    count_255_per_channel = np.sum(single_image == 255, axis=(0, 1))  # Sum along height and width
                    print(f"255 count per channel: {count_255_per_channel}")
                else:
                    print(f"The number of 255 values in the array: {count_255_per_channel}")
                print("Image dtype in extract_contours:", single_image.dtype)
                single_image = single_image.transpose((1, 2, 0))

                # Ensure the image has 3 channels
                #if len(single_image.shape) != 3 or single_image.shape[-1] != 3:
                 #   raise ValueError(f"Unexpected single_image shape: {single_image.shape}")

                # Debug: Test cvtColor manually
                #print("Testing cv2.cvtColor on single_image...")
                #gray = cv2.cvtColor(single_image, cv2.COLOR_BGR2GRAY)
                #print("Grayscale conversion successful!")


                contour = extract_contours(single_image)
                # triangle_contour_path = "./data/triangle_contour.npy"
                # contour = np.load(triangle_contour_path)  # Load as a NumPy array
                
                
                
                # # sys.exit()
                # contour = torch.from_numpy(contour).float()  # Convert to a PyTorch tensor
                # if not isinstance(contour_data, np.ndarray):
                #     raise TypeError(f"Expected contour data as a NumPy array, but got {type(contour_data)}.")
                #
                # contour = torch.from_numpy(contour_data).float()  # Convert to PyTorch tensor

                # contour = extract_contours(single_image)
                print(f"Contour (NumPy) data type: {contour.dtype}")
                print(f"Contour (NumPy) shape: {contour.shape}")
                print(f"Contour (NumPy) contents: {contour[:5]}")  # Print the first 5 rows for example

                # contour = torch.from_numpy(contour).float()
                print(f"Contour (PyTorch) data type: {contour.dtype}")
                print(f"Contour (PyTorch) shape: {contour.shape}")
                print(f"Contour (PyTorch) contents: {contour[:5]}")  # Print the first 5 rows for example

                #contour = extract_contours(object_image[object_idx].transpose((1, 2, 0)))
                contour = torch.from_numpy(contour).float()
                object_vertices.append(contour)
                # object_vertices stores the contour data for each object as a list of tensors
            object_vertices = torch.stack(object_vertices, dim=0)
            object_vertices[..., 0] = (object_vertices[..., 0] - object_pts_min_x) / (
                        object_pts_max_x - object_pts_min_x) * 2.0 - 1.0
            object_vertices[..., 1] = (object_vertices[..., 1] - object_pts_min_y) / (
                        object_pts_max_y - object_pts_min_y) * 2.0 - 1.0
            # dimension [..., 0] and [..., 1] represents all the data from x (0) axis and y (1) axis and are normalized to [-1, 1]
    else:
    # We have the classifier guidance in bash command, so we do not care about this.
        classifier_model = None
        object_vertices = None
        object_ids = None
    diffusion_model = Diffusion(noise_pred_net=unet, noise_scheduler=scheduler,
                                num_inference_steps=args.num_inference_steps, mode=mode, input_dim=input_dim,
                                num_points=num_spline_points, learning_rate=args.learning_rate,
                                lr_warmup_steps=args.lr_warmup_steps, ema_power=args.ema_power,
                                class_cond=args.classifier_guidance, classifier_model=classifier_model,
                                grid_size=args.grid_size, num_pos=args.num_pos, object_vertices=object_vertices,
                                object_ids=object_ids, num_cpus=args.num_cpus, pts_x_dim=pts_x_dim, pts_z_dim=pts_z_dim,
                                sub_batch_size=args.sub_bs, render_video=args.render_video, seed=args.seed)

    os.makedirs(args.save_dir, exist_ok=True)
    project_name = 'classifier_guidance_fixed' if args.classifier_guidance else 'gripper_diffusion'
    wandb_logger = WandbLogger(project=project_name, log_model='all', save_dir=args.save_dir, name=mode)
    callbacks = []
    if rank_idx == 0:
        callbacks.extend(
            (
                ModelCheckpoint(
                    dirpath=f"{args.save_dir}/checkpoints/",  # type: ignore
                    filename="{epoch:04d}",
                    every_n_epochs=1,
                    save_last=True,
                    save_top_k=10,
                    monitor="epoch",
                    mode="max",
                    save_weights_only=False,
                ),
                RichProgressBar(leave=True),
                LearningRateMonitor(logging_interval="step"),
            )
        )
    trainer = LightningTrainer(accelerator='gpu', devices=1, num_nodes=1, check_val_every_n_epoch=args.val_step,
                               log_every_n_steps=1, max_epochs=args.num_epochs, logger=wandb_logger,
                               default_root_dir=args.save_dir, callbacks=callbacks, inference_mode=False)
    # shortcut for inference only
    if args.mode == 'test':
        trainer.validate(diffusion_model, test_loader, ckpt_path=args.diffusion_checkpoint_path)
        return

    if args.diffusion_checkpoint_path is not None:
        print('loading diffusion checkpoint from', args.diffusion_checkpoint_path)
        trainer.fit(diffusion_model, train_loader, val_loader, ckpt_path=args.diffusion_checkpoint_path)
    else:
        trainer.fit(diffusion_model, train_loader, val_loader)


if __name__ == "__main__":
    args = parse()
    train(args)