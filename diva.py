from abc import abstractmethod
from typing import List, Callable, Union, Any, TypeVar, Tuple
from itertools import cycle
Tensor = TypeVar('torch.tensor')

import os
import torch
import numpy as np
from torch import optim, nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Subset
from torchvision import transforms
import torchvision.utils as vutils
from torchvision.datasets import MNIST,FashionMNIST
# from dataset.reuters10k.reuters10k import Reuters10kDataset
import pytorch_lightning as pl
from scipy.optimize import linear_sum_assignment

# make sure you have configured bnpy package in your environment
# refer to the link: https://bnpy.readthedocs.io/en/latest/
import bnpy
from bnpy.data.XData import XData

class BaseVAE(nn.Module):
    
    def __init__(self) -> None:
        super(BaseVAE, self).__init__()

    def encode(self, input: Tensor) -> List[Tensor]:
        raise NotImplementedError

    def decode(self, input: Tensor) -> Any:
        raise NotImplementedError

    def sample(self, batch_size:int, current_device: int, **kwargs) -> Tensor:
        raise RuntimeWarning()

    def generate(self, x: Tensor, **kwargs) -> Tensor:
        raise NotImplementedError

    @abstractmethod
    def forward(self, *inputs: Tensor) -> Tensor:
        pass

    @abstractmethod
    def loss_function(self, *inputs: Any, **kwargs) -> Tensor:
        pass


# for image dataset
class DIVA(BaseVAE):

    def __init__(self,
                 in_channels: int,
                 latent_dim: int,
                 dpmm_param: dict,
                 hidden_dims: List = None,
                 **kwargs) -> None:
        super(DIVA, self).__init__()

        self.latent_dim = latent_dim
        self.dpmm_param = dpmm_param

        modules = []
        if hidden_dims is None:
            hidden_dims = [32, 64]
        self.hidden_dims = hidden_dims

        # Build Encoder
        for h_dim in hidden_dims:
            modules.append(
                nn.Sequential(
                    nn.Conv2d(in_channels, out_channels=h_dim,
                              kernel_size=4, stride=2, padding=1),
                    nn.BatchNorm2d(h_dim),
                    nn.LeakyReLU())
            )
            in_channels = h_dim

        self.encoder = nn.Sequential(*modules)

        flatten_dim = hidden_dims[-1]*7*7
        self.fc_mu = nn.Linear(flatten_dim, latent_dim)
        self.fc_log_var = nn.Linear(flatten_dim, latent_dim)


        # Build Decoder
        modules = []

        self.decoder_input = nn.Linear(latent_dim, flatten_dim)

        hidden_dims.reverse()
        hidden_dims = [hidden_dims[0]] + hidden_dims

        for i in range(len(hidden_dims) - 1): # TODO： changeback to -1
            modules.append(
                nn.Sequential(
                    nn.ConvTranspose2d(hidden_dims[i],
                                       hidden_dims[i + 1],
                                       kernel_size=4,
                                       stride=2,
                                       padding=1,
                                       output_padding=0),
                    nn.BatchNorm2d(hidden_dims[i + 1]),
                    nn.LeakyReLU())
            )



        self.decoder = nn.Sequential(*modules)

        self.final_layer = nn.Sequential(
                            nn.ConvTranspose2d(hidden_dims[-1],
                                               1,
                                               kernel_size=3,
                                               stride=1,
                                               padding=1,
                                               output_padding=0),
                            nn.Tanh())
        # Build DPMM
        self.bnp_model = None
        self.bnp_info_dict = None
        pwd = os.getcwd()
        self.bnp_root = pwd + '/save/bn_model/'
        self.bnp_iterator = cycle(range(2))

    def encode(self, input: Tensor) -> List[Tensor]:
        """
        Encodes the input by passing through the encoder network
        and returns the latent codes.
        :param input: (Tensor) Input tensor to encoder [N x C x H x W]
        :return: (Tensor) List of latent codes
        """
        result = self.encoder(input)
        result = torch.flatten(result, start_dim=1)
        mu = self.fc_mu(result)
        log_var = self.fc_log_var(result)
        return [mu, log_var]

    def decode(self, z: Tensor) -> Tensor:
        """
        Maps the given latent codes
        onto the image space.
        :param z: (Tensor) [B x D]
        :return: (Tensor) [B x C x H x W]
        """
        result = self.decoder_input(z)
        result = result.view(-1, self.hidden_dims[0], 7, 7) 
        result = self.decoder(result)
        result = self.final_layer(result)
        return result

    def reparameterize(self, mu: Tensor, log_var: Tensor) -> Tensor:
        """
        Reparameterization trick to sample from N(mu, var) from
        N(0,1).
        :param mu: (Tensor) Mean of the latent Gaussian [B x D]
        :param logvar: (Tensor) Standard deviation of the latent Gaussian [B x D]
        :return: (Tensor) [B x D]
        """
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)
        return eps * std + mu

    def forward(self, input: Tensor, **kwargs) -> List[Tensor]:
        mu, log_var = self.encode(input)
        z = self.reparameterize(mu, log_var)
        return  [self.decode(z), input, mu, log_var, z] # [recon, input, mu, log_var, z]

    def loss_function(self,
                      *args,
                      **kwargs) -> dict:
        """
        Computes the VAE loss function.
        KL(N(\mu, \sigma), N(0, 1)) = \log \frac{1}{\sigma} + \frac{\sigma^2 + \mu^2}{2} - \frac{1}{2}
        :param args:
        :param kwargs:
        :return:
        """
        recons = args[0]
        input = args[1]
        mu = args[2]
        log_var = args[3]
        z = args[4]  # batch_size * latent_dim

        recons_loss = F.mse_loss(recons, input)

        # calculate kl divergence
        kld_weight = kwargs['M_N'] # Account for the minibatch samples from the dataset
        # M_N = self.params['batch_size']/ self.num_train_imgs,
        if not self.bnp_model:
            kld_loss = torch.mean(-0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1), dim = 0)
            loss = recons_loss + kld_weight * kld_loss
            return {'loss': loss, 'reconstruction_loss':recons_loss, 'kld_loss': kld_loss, 'z': z}
        else:
            prob_comps, comps = self.cluster_assignments(z) # prob_comps --> resp, comps --> Z[n]
            # get a distribution of the latent variables 
            var = torch.exp(0.5 * log_var)**2
            # batch_shape [batch_size], event_shape [latent_dim]
            dist = torch.distributions.MultivariateNormal(loc=mu, 
                                                          covariance_matrix=torch.diag_embed(var))

            # get a distribution for each cluster
            B, K = prob_comps.shape # batch_shape, number of active clusters
            kld = torch.zeros(B).to(mu.device)
            for k in range(K):
              # batch_shape [], event_shape [latent_dim]
              prob_k = prob_comps[:, k]
              dist_k = torch.distributions.MultivariateNormal(loc=self.comp_mu[k].to(mu.device), 
                                                            covariance_matrix=torch.diag_embed(self.comp_var[k]).to(mu.device))
              # batch_shape [batch_size], event_shape [latent_dim]
              expanded_dist_k = dist_k.expand(dist.batch_shape)

              kld_k = torch.distributions.kl_divergence(dist, expanded_dist_k)   #  shape [batch_shape, ]
              kld += torch.from_numpy(prob_k).to(mu.device) * kld_k
              
            kld_loss = torch.mean(kld)

            loss = recons_loss + kld_weight * kld_loss
            loss = loss.to(input.device)
            return {'loss': loss, 'reconstruction_loss':recons_loss, 'kld_loss': kld_loss, 'z': z, 'comps': comps}


    def sample(self, 
               num_samples:int,
               current_device: int, **kwargs) -> Tensor:
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :param current_device: (Int) Device to run the model
        :return: (Tensor)
        """
        z = torch.randn(num_samples,
                        self.latent_dim)

        z = z.to(current_device)

        samples = self.decode(z)
        return samples

    def sample_component(self,
               num_samples:int,
               component:int,
               current_device: int, 
               **kwargs) -> Tensor:
        """
        Samples from a dpmm cluster and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :param current_device: (Int) Device to run the model
        :return: (Tensor)          
        """
        mu = self.comp_mu[component]
        cov = torch.diag_embed(self.comp_var[component])
        dist = torch.distributions.MultivariateNormal(loc=mu, 
                                                      covariance_matrix=cov)
        z = dist.sample_n(num_samples)
        z = z.to(current_device)

        samples = self.decode(z)
        return samples


    def generate(self, x: Tensor, **kwargs) -> Tensor:
        """
        Given an input image x, returns the reconstructed image
        :param x: (Tensor) [B x C x H x W]
        :return: (Tensor) [B x C x H x W]
        """

        return self.forward(x)[0]

    def fit_dpmm(self, z):
        z = XData(z.detach().cpu().numpy())
        if not self.bnp_model:
          print("Initialing DPMM model ...")
          self.bnp_model, self.bnp_info_dict = bnpy.run(z, 'DPMixtureModel', 'DiagGauss', 'memoVB', 
                                                        output_path = self.bnp_root+str(next(self.bnp_iterator)),
                                                        initname='randexamples',
                                                        K=1, 
                                                        gamma0 = 5.0, 
                                                        sF=0.1, 
                                                        ECovMat='eye',
                                                        b_Kfresh=5, b_startLap=0, m_startLap=2,
                                                        # moves='birth,delete,merge,shuffle', 
                                                        moves='birth,merge,shuffle', 
                                                        b_minNumAtomsForNewComp=self.dpmm_param['b_minNumAtomsForNewComp'],
                                                        b_minNumAtomsForTargetComp=self.dpmm_param['b_minNumAtomsForTargetComp'],
                                                        b_minNumAtomsForRetainComp=self.dpmm_param['b_minNumAtomsForRetainComp'],
                                                        nLap=2)
        else: 
          self.bnp_model, self.bnp_info_dict = bnpy.run(z, 'DPMixtureModel', 'DiagGauss', 'memoVB', 
                                                        output_path = self.bnp_root+str(next(self.bnp_iterator)),
                                                        initname=self.bnp_info_dict['task_output_path'],
                                                        K=self.bnp_info_dict['K_history'][-1],
                                                        gamma0=5.0,
                                                        b_Kfresh=5, b_startLap=1, m_startLap=2,
                                                        # moves='birth,delete,merge,shuffle',
                                                        moves='birth,merge,shuffle',  
                                                        b_minNumAtomsForNewComp=self.dpmm_param['b_minNumAtomsForNewComp'],
                                                        b_minNumAtomsForTargetComp=self.dpmm_param['b_minNumAtomsForTargetComp'],
                                                        b_minNumAtomsForRetainComp=self.dpmm_param['b_minNumAtomsForRetainComp'],
                                                        nLap=2)
        self.calc_cluster_component_params()


    def calc_cluster_component_params(self):
        self.comp_mu = [torch.Tensor(self.bnp_model.obsModel.get_mean_for_comp(i)) for i in np.arange(0, self.bnp_model.obsModel.K)]
        self.comp_var = [torch.Tensor(np.sum(self.bnp_model.obsModel.get_covar_mat_for_comp(i), axis=0)) for i in np.arange(0, self.bnp_model.obsModel.K)] 
        print("Log: comp_mu", self.comp_mu)  
        print("Log: comp_var", self.comp_var)

    def cluster_assignments(self, z):
        z = XData(z.detach().cpu().numpy())
        LP = self.bnp_model.calc_local_params(z)
        # Here, resp is a 2D array of size N x K. here N is batch size, K active clusters
        # Each entry resp[n, k] gives the probability 
        #that data atom n is assigned to cluster k under 
        # the posterior.
        resp = LP['resp'] 
        # To convert to hard assignments
        # Here, Z is a 1D array of size N, where entry Z[n] is an integer in the set {0, 1, 2, … K-1, K}.
        # Z represents for each atom n (in total N), which cluster it should belongs to accroding to the probability
        Z = resp.argmax(axis=1)
        return resp, Z


# for non-image dataset
class DIVA_MLP(BaseVAE):

    def __init__(self,
                 input_dim: int,
                 latent_dim: int,
                 dpmm_param: dict,
                 output_type: str='linear',
                 **kwargs) -> None:
        super(DIVA_MLP, self).__init__()

        self.latent_dim = latent_dim
        self.input_dim = input_dim
        self.dpmm_param = dpmm_param
        self.output_type = output_type

        self.encoder = nn.Sequential(
            nn.Linear(self.input_dim, 500),
            nn.ReLU(),
            nn.Linear(500, 500),
            nn.ReLU(),
            nn.Linear(500, 2000),
            nn.ReLU()
        )
        self.fc_mu = nn.Linear(2000, latent_dim)
        self.fc_log_var = nn.Linear(2000, latent_dim)

        # Build Decoder

        self.decoder = nn.Sequential(
            nn.Linear(self.latent_dim, 2000),
            nn.ReLU(),
            nn.Linear(2000, 500),
            nn.ReLU(),
            nn.Linear(500, 500),
            nn.ReLU(),
            nn.Linear(500, self.input_dim)
        )

        # Build DPMM
        self.bnp_model = None
        self.bnp_info_dict = None
        pwd = os.getcwd()
        self.bnp_root = pwd + 'save/bn_model/'
        self.bnp_iterator = cycle(range(2))

    def encode(self, input: Tensor) -> List[Tensor]:
        """
        Encodes the input by passing through the encoder network
        and returns the latent codes.
        :param input: (Tensor) Input tensor to encoder [N x C x H x W]
        :return: (Tensor) List of latent codes
        """
        result = self.encoder(input)
        mu = self.fc_mu(result)
        log_var = self.fc_log_var(result)
        return [mu, log_var]

    def decode(self, z: Tensor) -> Tensor:
        """
        Maps the given latent codes
        onto the image space.
        :param z: (Tensor) [B x D]
        :return: (Tensor) [B x C x H x W]
        """
        
        result = self.decoder(z)
        
        if self.output_type == 'linear':
            pass
        elif self.output_type == 'sigmoid':
            result = torch.sigmoid(result)
        else: # tahn
            result = torch.tanh(result)
        
        return result
    
    def loss_function(self,
                      *args,
                      **kwargs) -> dict:
        """
        Computes the VAE loss function.
        KL(N(\mu, \sigma), N(0, 1)) = \log \frac{1}{\sigma} + \frac{\sigma^2 + \mu^2}{2} - \frac{1}{2}
        :param args:
        :param kwargs:
        :return:
        """
        recons = args[0]
        input = args[1]
        mu = args[2]
        log_var = args[3]
        z = args[4]  # batch_size * latent_dim

        # reconstruction loss
        recons_loss = F.mse_loss(recons, input, reduction='sum')
        
        # recons_loss = F.mse_loss(recons * 255, input * 255, reduction="sum") / 255

        # recons_loss = F.mse_loss(recons, input, reduction='none')
        # recons_loss = recons_loss.sum(dim=[1,2,3]).mean(dim=[0])
        

        # calculate kl divergence
        kld_weight = kwargs['M_N'] # Account for the minibatch samples from the dataset
        # M_N = self.params['batch_size']/ self.num_train_imgs,
        if not self.bnp_model:
            kld_loss = torch.mean(-0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1), dim = 0)
            loss = recons_loss + kld_weight * kld_loss
            return {'loss': loss, 'reconstruction_loss':recons_loss, 'kld_loss': kld_loss, 'z': z}
        else:
            prob_comps, comps = self.cluster_assignments(z) # prob_comps --> resp, comps --> Z[n]
            # get a distribution of the latent variables 
            var = torch.exp(0.5 * log_var)**2
            # batch_shape [batch_size], event_shape [latent_dim]
            dist = torch.distributions.MultivariateNormal(loc=mu, 
                                                          covariance_matrix=torch.diag_embed(var))

            # get a distribution for each cluster
            B, K = prob_comps.shape # batch_shape, number of active clusters
            kld = torch.zeros(B).to(mu.device)
            for k in range(K):
              # batch_shape [], event_shape [latent_dim]
              prob_k = prob_comps[:, k]
              dist_k = torch.distributions.MultivariateNormal(loc=self.comp_mu[k].to(mu.device), 
                                                            covariance_matrix=torch.diag_embed(self.comp_var[k]).to(mu.device))
              # batch_shape [batch_size], event_shape [latent_dim]
              expanded_dist_k = dist_k.expand(dist.batch_shape)

              kld_k = torch.distributions.kl_divergence(dist, expanded_dist_k)   #  shape [batch_shape, ]
              kld += torch.from_numpy(prob_k).to(mu.device) * kld_k
              
            kld_loss = torch.mean(kld)

            loss = recons_loss + kld_weight * kld_loss
            loss = loss.to(input.device)
            return {'loss': loss, 'reconstruction_loss':recons_loss, 'kld_loss': kld_loss, 'z': z, 'comps': comps}

    def reparameterize(self, mu: Tensor, log_var: Tensor) -> Tensor:
        """
        Reparameterization trick to sample from N(mu, var) from
        N(0,1).
        :param mu: (Tensor) Mean of the latent Gaussian [B x D]
        :param logvar: (Tensor) Standard deviation of the latent Gaussian [B x D]
        :return: (Tensor) [B x D]
        """
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)
        return eps * std + mu

    def forward(self, input: Tensor, **kwargs) -> List[Tensor]:
        mu, log_var = self.encode(input)
        z = self.reparameterize(mu, log_var)
        return  [self.decode(z), input, mu, log_var, z] # [recon, input, mu, log_var, z]

    def sample(self, 
               num_samples:int,
               current_device: int, **kwargs) -> Tensor:
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :param current_device: (Int) Device to run the model
        :return: (Tensor)
        """
        z = torch.randn(num_samples,
                        self.latent_dim)

        z = z.to(current_device)

        samples = self.decode(z)
        return samples

    def sample_component(self,
               num_samples:int,
               component:int,
               current_device: int, 
               **kwargs) -> Tensor:
        """
        Samples from a dpmm cluster and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :param current_device: (Int) Device to run the model
        :return: (Tensor)          
        """
        mu = self.comp_mu[component]
        cov = torch.diag_embed(self.comp_var[component])
        dist = torch.distributions.MultivariateNormal(loc=mu, 
                                                      covariance_matrix=cov)
        z = dist.sample_n(num_samples)
        z = z.to(current_device)

        samples = self.decode(z)
        return samples

    def generate(self, x: Tensor, **kwargs) -> Tensor:
        """
        Given an input image x, returns the reconstructed image
        :param x: (Tensor) [B x C x H x W]
        :return: (Tensor) [B x C x H x W]
        """

        return self.forward(x)[0]

    def fit_dpmm(self, z):
        z = XData(z.detach().cpu().numpy())
        if not self.bnp_model:
          print("Initialing DPMM model ...")
          self.bnp_model, self.bnp_info_dict = bnpy.run(z, 'DPMixtureModel', 'DiagGauss', 'memoVB', 
                                                        output_path = self.bnp_root+str(next(self.bnp_iterator)),
                                                        initname='randexamples',
                                                        K=1, 
                                                        gamma0 = 5.0, 
                                                        sF=0.1, 
                                                        ECovMat='eye',
                                                        b_Kfresh=5, b_startLap=0, m_startLap=2,
                                                        moves='birth,merge,shuffle', 
                                                        # moves='birth,delete,merge,shuffle', 
                                                        nLap=2,
                                                        b_minNumAtomsForNewComp=self.dpmm_param['b_minNumAtomsForNewComp'],
                                                        b_minNumAtomsForTargetComp=self.dpmm_param['b_minNumAtomsForTargetComp'],
                                                        b_minNumAtomsForRetainComp=self.dpmm_param['b_minNumAtomsForRetainComp'],
                                                       )
        else: 
          self.bnp_model, self.bnp_info_dict = bnpy.run(z, 'DPMixtureModel', 'DiagGauss', 'memoVB', 
                                                        output_path = self.bnp_root+str(next(self.bnp_iterator)),
                                                        initname=self.bnp_info_dict['task_output_path'],
                                                        K=self.bnp_info_dict['K_history'][-1],
                                                        gamma0=5.0,
                                                        sF=self.dpmm_param['sF'],
                                                        b_Kfresh=5, b_startLap=1, m_startLap=2,
                                                        moves='birth,merge,shuffle', 
                                                        # moves='birth,delete,merge,shuffle', 
                                                        nLap=2,
                                                        b_minNumAtomsForNewComp=self.dpmm_param['b_minNumAtomsForNewComp'],
                                                        b_minNumAtomsForTargetComp=self.dpmm_param['b_minNumAtomsForTargetComp'],
                                                        b_minNumAtomsForRetainComp=self.dpmm_param['b_minNumAtomsForRetainComp'],
                                                       )
        self.calc_cluster_component_params()

    def calc_cluster_component_params(self):
        self.comp_mu = [torch.Tensor(self.bnp_model.obsModel.get_mean_for_comp(i)) for i in np.arange(0, self.bnp_model.obsModel.K)]
        self.comp_var = [torch.Tensor(np.sum(self.bnp_model.obsModel.get_covar_mat_for_comp(i), axis=0)) for i in np.arange(0, self.bnp_model.obsModel.K)] 
        print("Log: comp_mu", self.comp_mu)  
        print("Log: comp_var", self.comp_var)

    def cluster_assignments(self, z):
        z = XData(z.detach().cpu().numpy())
        LP = self.bnp_model.calc_local_params(z)
        # Here, resp is a 2D array of size N x K. here N is batch size, K active clusters
        # Each entry resp[n, k] gives the probability 
        #that data atom n is assigned to cluster k under 
        # the posterior.
        resp = LP['resp'] 
        # To convert to hard assignments
        # Here, Z is a 1D array of size N, where entry Z[n] is an integer in the set {0, 1, 2, … K-1, K}.
        # Z represents for each atom n (in total N), which cluster it should belongs to accroding to the probability
        Z = resp.argmax(axis=1)
        return resp, Z
    

def data_loader(fn):
    """
    Decorator to handle the deprecation of data_loader from 0.7
    :param fn: User defined data loader function
    :return: A wrapper for the data_loader function
    """

    def func_wrapper(self):
        return fn(self)

    return func_wrapper


# DIVA Training Manager
class DIVA_Experiment(pl.LightningModule):

    def __init__(self,
                 vae_model: BaseVAE,
                 params: dict) -> None:
        super(DIVA_Experiment, self).__init__()

        self.model = vae_model
        self.params = params
        self.curr_device = None
        self.hold_graph = False
        self.assignment = None
        try:
            self.hold_graph = self.params['retain_first_backpass']
        except:
            pass

        self.dpmm_init_epoch = 0   # Instead of fitting DPMM from the first epoch, pre-train the encoder for a few epochs


    def forward(self, input: Tensor, **kwargs) -> Tensor:
        return self.model(input, **kwargs)

    def training_step(self, batch, batch_idx, optimizer_idx = 0):
        real_img, labels = batch
        self.curr_device = real_img.device

        results = self.forward(real_img, labels = labels)
        train_loss = self.model.loss_function(*results,
                                            #   M_N = self.params['batch_size']/ self.num_train_imgs,
                                              M_N = self.params['kld_weight'],
                                              optimizer_idx=optimizer_idx,
                                              batch_idx = batch_idx,
                                              device = self.curr_device)
        for name, metric in train_loss.items():
          if "loss" in name:
              self.log("train_" + name, metric.item(), on_step=False, on_epoch=True, prog_bar=True)

        train_loss.update({'labels': labels})

        return train_loss    # latent encoding

    def training_epoch_end(self, outputs):
        if self.current_epoch >= self.dpmm_init_epoch:
          z = torch.cat([outputs[i]['z'] for i in range(0, len(outputs))])
          self.model.fit_dpmm(z)

        if "comps" in outputs[0]:
            comps = np.array([outputs[i]['comps'] for i in range(0, len(outputs))]).flatten()
            labels = torch.cat([outputs[i]['labels'] for i in range(0, len(outputs))]).cpu()
            acc = self.classification_accuracy(comps, labels)
            acc2, _ = self.unsupervised_clustering_accuracy(labels.numpy(), comps)
            self.log("train_classification_acc", acc, on_step=False, on_epoch=True, prog_bar=True)
            self.log("train_clustering_acc", acc2, on_step=False, on_epoch=True, prog_bar=True)
            self.log("Number_of_DP_Comps", self.model.bnp_model.obsModel.K, on_step=False, on_epoch=True, prog_bar=True)
            
    
    def classification_accuracy(self, comps, targets):
        d = {}
        for comp, target in zip(comps, targets):
            if comp not in d:
                d[comp] = [target]
            else:
                d[comp].append(target)

        correct = 0
        for comp in d:
            task, count = np.unique(d[comp], return_counts=True)
            correct += max(count)
        acc = correct / len(comps)
        return acc

    def validation_step(self, batch, batch_idx, optimizer_idx = 0):
        real_img, labels = batch
        self.curr_device = real_img.device

        results = self.forward(real_img, labels = labels)
        val_loss = self.model.loss_function(*results,
                                            # M_N = self.params['batch_size']/ self.num_val_imgs,
                                            M_N = self.params['kld_weight'],
                                            optimizer_idx = optimizer_idx,
                                            batch_idx = batch_idx,
                                            device = self.curr_device)
        for name, metric in val_loss.items():
          if "loss" in name:
            self.log("val_" + name, metric.item(), on_step=False, on_epoch=True, prog_bar=True)
        val_loss.update({'labels': labels})
        
        return val_loss

    def validation_epoch_end(self, outputs):
        self.sample_images()

        if "comps" in outputs[0]:
            comps = comps = np.array([outputs[i]['comps'] for i in range(0, len(outputs))]).flatten()
            labels = torch.cat([outputs[i]['labels'] for i in range(0, len(outputs))]).cpu()
            acc = self.classification_accuracy(comps, labels)
            acc2, _ = self.unsupervised_clustering_accuracy(labels.numpy(), comps)
            self.log("test_classification_acc", acc, on_step=False, on_epoch=True, prog_bar=True)
            self.log("test_cluster_acc", acc2, on_step=False, on_epoch=True, prog_bar=True)

    def sample_images(self):
        try:
          for k in range(0, len(self.model.comp_mu)):
            samples = self.model.sample_component(16, k,  self.curr_device)
            samples = samples.cpu()
            vutils.save_image(samples.data,
                              f"save/imgs/sampled_{k}.pdf", 
                              normalize=True, 
                              nrow=4)
        except Exception as e: 
          print("ERROR: Failed sampling images")
          print(e)

    def configure_optimizers(self):

        optims = []
        scheds = []

        optimizer = optim.Adam(self.model.parameters(),
                               lr=self.params['LR'],
                               weight_decay=self.params['weight_decay'])
        optims.append(optimizer)
        # Check if more than 1 optimizer is required (Used for adversarial training)
        try:
            if self.params['LR_2'] is not None:
                optimizer2 = optim.Adam(getattr(self.model,
                                                self.params['submodel']).parameters(), 
                                                lr=self.params['LR_2'])
                optims.append(optimizer2)
        except:
            pass

        try:
            if self.params['scheduler_gamma'] is not None:
                scheduler = optim.lr_scheduler.ExponentialLR(optims[0],
                                                             gamma = self.params['scheduler_gamma'])
                scheds.append(scheduler)

                # Check if another scheduler is required for the second optimizer
                try:
                    if self.params['scheduler_gamma_2'] is not None:
                        scheduler2 = optim.lr_scheduler.ExponentialLR(optims[1],
                                                                      gamma = self.params['scheduler_gamma_2'])
                        scheds.append(scheduler2)
                except:
                    pass
                return optims, scheds
        except:
            return optims

    @data_loader
    def train_dataloader(self):
        transform = self.data_transforms()

        if self.params['dataset'] == 'mnist':
            full_train_dataset = MNIST(root=self.params['data_path'],
                                       train=True,
                                       transform=transform,
                                       download=True,
            )

        elif self.params['dataset'] == 'fashion-mnist':
            full_train_dataset = FashionMNIST(root = self.params['data_path'],
                            train = True, 
                            transform = transform, 
                            download = True,
                            )
        elif self.params['dataset'] == 'reuters10k':
            # full_train_dataset = Reuters10kDataset(train=True)
            pass
        else:
            raise ValueError('Undefined dataset type')
        # split subset
        dataset = self.configure_subset(full_train_dataset, self.params['num_digits'])
        self.num_train_imgs = len(dataset)
        return DataLoader(dataset,
                          batch_size = self.params['batch_size'],
                          shuffle = True, 
                          drop_last=True)

    @data_loader
    def val_dataloader(self):
        transform = self.data_transforms()

        if self.params['dataset'] == 'mnist':
            full_test_dataset = MNIST(root=self.params['data_path'],
                                       train=False,
                                       transform=transform,
                                       download=True,
            )
        
        elif self.params['dataset'] == 'fashion-mnist':
            full_test_dataset = FashionMNIST(root = self.params['data_path'],
                                        train = False,
                                        transform=transform, 
                                        download=True,
                                        )            

        elif self.params['dataset'] == 'reuters10k':
            # full_test_dataset = Reuters10kDataset(train=False)
            pass
        else:
            raise ValueError('Undefined dataset type')
        # split subset
        dataset = self.configure_subset(full_test_dataset, self.params['num_digits'])
        self.sample_dataloader =  DataLoader(dataset,
                                            batch_size= self.params['batch_size'], 
                                            shuffle = False, 
                                            drop_last=True
                                            )
        self.num_val_imgs = len(self.sample_dataloader)
        return self.sample_dataloader
    
    def configure_subset(self, dataset, num_digits:int):
        '''
        limit the representations (type of digits) in the dataset, to build a subset 
        e.g. num_digits = 3, the subset should only contents digits [0,1,2]
        '''
        full_features_num = 4 if self.params['dataset'] not in ['reuters10k'] else 10

        if num_digits < full_features_num:
            digits = list(np.arange(num_digits))
            select_idxs = [i for i in range(len(dataset)) if dataset.targets[i] in digits]
            subset = Subset(dataset, select_idxs)
        else:
            subset = dataset
        return subset

    def data_transforms(self):

        transform = transforms.Compose([transforms.ToTensor(),
                                            transforms.Normalize((0.1307,), (0.3081,))])
        return transform
    
    def unsupervised_clustering_accuracy(self, y: Union[np.ndarray, torch.Tensor], y_pred: Union[np.ndarray, torch.Tensor]) -> tuple:
        """Unsupervised Clustering Accuracy
        """
        assert len(y_pred) == len(y)
        u = np.unique(y)
        n_true_clusters = len(u)
        v = np.unique(y_pred)
        n_pred_clusters = len(v)
        map_u = dict(zip(u, range(n_true_clusters)))
        map_v = dict(zip(v, range(n_pred_clusters)))
        inv_map_u = {v: k for k, v in map_u.items()}
        inv_map_v = {v: k for k, v in map_v.items()}
        r = np.zeros((n_pred_clusters, n_true_clusters), dtype=np.int64)
        for y_pred_, y_ in zip(y_pred, y):
            if y_ in map_u:
                r[map_v[y_pred_], map_u[y_]] += 1
        reward_matrix  = np.concatenate((r, r, r), axis=1)
        cost_matrix = reward_matrix.max() - reward_matrix
        row_assign, col_assign = linear_sum_assignment(cost_matrix)

        # Construct optimal assignments matrix
        row_assign = row_assign.reshape((-1, 1))  # (n,) to (n, 1) reshape
        col_assign = col_assign.reshape((-1, 1))  # (n,) to (n, 1) reshape
        assignments = np.concatenate((row_assign, col_assign), axis=1)
        assignments = [[inv_map_v[x], inv_map_u[y%n_true_clusters]] for x, y in assignments]

        optimal_reward = reward_matrix[row_assign, col_assign].sum() * 1.0
        return optimal_reward / y_pred.size, assignments 