# Consistent-Attack

- This is the official implementation for [Consistent Attack: Universal Adversarial Perturbation on Embodied Vision Navigation](https://arxiv.org/pdf/2206.05751.pdf) (Accepted in Pattern Recognition Letters (PRL) 2023).

- The code is based on [Habitat-Lab](https://github.com/facebookresearch/habitat-lab)

## Installation

```
git clone this repo

cd Consistent-Attack
conda create -n consistent-attack python=3.7
conda activate consistent-attack
```

Then you need to install [habitat-sim](https://github.com/facebookresearch/habitat-sim#installation) and then 

```
pip install -e habitat-lab
pip install -e habitat-baselines
pip install numpy
pip install torch
```

## Usage

Then you can run the attack code like
```
cd habitat-baselines/habitat-baselines

xvfb-run -a python run.py --exp-config config/pointnav/ppo_pointnav_test_d.yaml --run-type eval habitat_baselines.torch_gpu_id 0 habitat_baselines.simulator_gpu_id 0 habitat_baselines.eval.evaluate_strategy 1 habitat_baselines.eval.update_num 5 habitat_baselines.eval.traj_num_each 1
```

here we do not attack the model by setting habitat_baselines.eval.evaluate_stratege=0, use UAP by setting habitat_baselines.eval.evaluate_stratege=1, use Reward UAP by setting habitat_baselines.eval.evaluate_stratege=2, use Trajectory UAP by setting habitat_baselines.eval.evaluate_stratege=3

Pretrained models are in the fold "/habitat-baselines/model/" and are from Habitat-baselines in [Habitat-Lab](https://github.com/facebookresearch/habitat-lab).

Scene datasets are in the fold "/habitat-baselines/scene_data/". We only provide scene datasets of habitat-test from [Habitat-Lab](https://github.com/facebookresearch/habitat-lab). The dataset of gibson and mp3d can be downloaded by the instructions [here](https://github.com/facebookresearch/habitat-lab/blob/main/DATASETS.md).

## Citation

If you find Consistent Attack helpful, please cite our paper.

```
@article{ying2023consistent,
  title={Consistent Attack: Universal Adversarial Perturbation on Embodied Vision Navigation},
  author={Ying, Chengyang and Qiaoben, You and Zhou, Xinning and Su, Hang and Ding, Wenbo and Ai, Jianyong},
  journal={Pattern Recognition Letters},
  year={2023},
  publisher={Elsevier}
}
```
