# DIVA: A Dirichlet Process Based Incremental Deep Clustering Algorithm via Variational Auto-Encoder
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://github.com/facebookresearch/mtrl/blob/main/LICENSE)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/release/python-370/)

Official implementation for paper: 
[DIVA: A Dirichlet Process Based Incremental Deep Clustering Algorithm via Variational Auto-Encoder](https://arxiv.org/abs/2305.14067)
<p align="center">
  A demo video for showing DIVA's dynamic adaptation ability in deep clustering.
  <a href="https://www.youtube.com/watch?v=uHPGAUSSbh8">
    <img src="https://github.com/Ghiara/diva/blob/master/pretrained/poster.png" alt="Demo Video" width="100%">
  </a>
</p>
<!-- </img> -->


## Requirements
we use python 3.7 and pytorch-lightning for training. Before start training, make sure you have installed bnpy package in your local environment, refer to [here](https://bnpy.readthedocs.io/en/latest/) for more details.

- python 3.7
- bnpy 1.7.0
- pytorch-lightning

## Installation Instructions

```
# Install dependencies and package
pip3 install -r requirements.txt
```

## Detailed Code Structure Overview

```
DIVA
  |- dataset                # folder for saving datasets
  |    |- reuters10k.py     # dataset instance of reuters10k that follows torchvision formatting
  |    |- reuters10k.mat    # origin data of reuters10k
  |- pretrained             # folder for saving pretrained example model on MNIST
  |    |- dpmm              # folder for saving DPMM cluster module
  |    |- diva_vae.ckpt     # checkpoint file of trained DIVA VAE part
  |    |- pretrained.ipynb  # example file how to load pretrained model
  |- diva.py                # diva implementations for image and text; train manager
  |- main.py                # main entry point of diva training, including evaluation plots.

```

## Load pretrained model
```
# load DPMM module
dpmm_model = bnpy.ioutil.ModelReader.load_model_at_prefix('path/to/your/bn_model/folder/dpmm', prefix="Best")

# function for getting the cluster parameters
def calc_cluster_component_params(bnp_model):
        comp_mu = [torch.Tensor(bnp_model.obsModel.get_mean_for_comp(i)) for i in np.arange(0, bnp_model.obsModel.K)]
        comp_var = [torch.Tensor(np.sum(bnp_model.obsModel.get_covar_mat_for_comp(i), axis=0)) for i in np.arange(0, bnp_model.obsModel.K)] 
        return comp_mu, comp_var
```

## Citation
if you would like to refer to our work, please use following BibTeX formatted citation
```
@misc{bing2023diva,
      title={DIVA: A Dirichlet Process Based Incremental Deep Clustering Algorithm via Variational Auto-Encoder}, 
      author={Zhenshan Bing and Yuan Meng and Yuqi Yun and Hang Su and Xiaojie Su and Kai Huang and Alois Knoll},
      year={2023},
      eprint={2305.14067},
      archivePrefix={arXiv},
      primaryClass={cs.LG}
}
```