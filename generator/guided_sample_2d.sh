python generator/train.py --mode='test' --checkpoint_path='ckpts/dynamics_2d.pt' \
    --classifier_guidance --diffusion_checkpoint_path='ckpts/diffusion_2d.ckpt' --object_dir='data/YellowTriangle.npy' --save_dir='models/2d/YELLOWTRIANGLE2'\
    --ctrlpts_dim=14 --num_fingers=2 --grid_size=360 --num_pos=5 --object_max_num_vertices=100 \
    --num_workers=0 --num_train_timesteps=15 --num_inference_steps=5 --ema_power=0.85 --batch_size=2  --num_cpus=8 --seed=2