# standard library imports
import os
import math
from math import sqrt, pi

# package imports
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions.normal import Normal
from torch.distributions.gamma import Gamma
from torch.distributions.multivariate_normal import MultivariateNormal
import tensorflow as tf
import tensorflow_probability as tfp

# local imports
import bnn.layers.sparse as layers
import bnn.inference
import bnn.util as util

class BayesLinearLasso(nn.Module):
    """
    Linear regression with double expential prior
    """
    def __init__(self, dim_in, dim_out, prior_w2_sig2=1.0, noise_sig2=1.0, scale_global=1.0, groups=None, scale_groups=None):
        super(BayesLinearLasso, self).__init__()

        ### architecture
        self.dim_in = dim_in
        self.dim_out = dim_out
        self.prior_w2_sig2 = prior_w2_sig2
        self.noise_sig2 = noise_sig2
        self.scale_global = scale_global
        self.groups = groups # list of lists with grouping (e.g. [[1,2,3], [4,5]])
        self.scale_groups = scale_groups

        
    def make_unnormalized_log_prob_tf(self, x, y):

        # Convert to tensors
        y_tf = tf.convert_to_tensor(y)
        x_tf = tf.convert_to_tensor(x)
        scale_global_tf = tf.dtypes.cast(tf.convert_to_tensor(self.scale_global), tf.float64)

        if self.groups is not None:
            groups_tf = [tf.convert_to_tensor(group) for group in self.groups]

        @tf.function
        def unnormalized_log_prob(w):
            resid = y_tf - x_tf@w

            # likelihood and L2 penalty
            log_prob = -1/self.noise_sig2*tf.transpose(resid)@(resid) \
                       - tf.transpose(w)@(1/self.prior_w2_sig2*tf.eye(self.dim_in, dtype=tf.float64))@w 
            
            # Within group
            log_prob -= tf.math.reduce_sum(scale_global_tf*tf.math.abs(w)) # L1 penalty
            #log_prob -= tf.math.reduce_sum(scale_global_tf*w**2) # L2 penalty
            

            # Group level
            if self.groups is not None:
                for scale_groups, group in zip(self.scale_groups, groups_tf):
                    log_prob -= scale_groups*tf.norm(tf.gather(w, group)) # L1 penalty
                    #log_prob -= scale_groups*tf.norm(tf.gather(w, group)**2) # L2 penalty

            return log_prob[0,0]

        return unnormalized_log_prob

    def train(self, x, y, num_results = int(10e3), num_burnin_steps = int(1e3)):
        '''
        Train with HMC
        '''
        unnormalized_log_prob_tf = self.make_unnormalized_log_prob_tf(x, y)
        init_values = .1*np.random.randn(self.dim_in,1)

        samples, accept = bnn.inference.mcmc.hmc_tf(unnormalized_log_prob_tf, 
            init_values, 
            num_results, 
            num_burnin_steps, 
            num_leapfrog_steps=3, 
            step_size=1.)

        return samples, accept

class RffGradPen(nn.Module):
    """
    Random features layer

    Variance of output layer scaled by width (see RFF activation function)
    """
    def __init__(self, dim_in, dim_hidden, dim_out, prior_w2_sig2=1.0, noise_sig2=1.0, scale_global=1.0, groups=None, scale_groups=None, lengthscale=1.0, penalty_type='l1'):
        super(RffGradPen, self).__init__()

        ### architecture
        self.dim_in = dim_in
        self.dim_hidden = dim_hidden
        self.dim_out = dim_out
        self.prior_w2_sig2 = prior_w2_sig2
        self.noise_sig2 = noise_sig2
        self.scale_global = scale_global
        self.groups = groups # list of lists with grouping (e.g. [[1,2,3], [4,5]])
        self.scale_groups = scale_groups
        self.lengthscale = lengthscale
        self.penalty_type = penalty_type

        self.register_buffer('w', torch.empty(dim_hidden, dim_in))
        self.register_buffer('b', torch.empty(dim_hidden))

        self.sample_features()

        self.act = lambda z: sqrt(2/self.dim_hidden)*torch.cos(z)

    def sample_features(self):
        # sample random weights for RFF features
        self.w.normal_(0, 1 / self.lengthscale)
        self.b.uniform_(0, 2*pi)
        
    def hidden_features(self, x):
        #return self.act(x@self.w.T + self.b.reshape(1,-1)) # (n, dim_hidden)
        return self.act(F.linear(x, self.w, self.b)) # (n, dim_hidden)

    def compute_jacobian(self, x):
        '''
        Compute jacobian of hidden units with respect to inputs. 
        Assumes inputs do not impact each other (i.e. input observation n only impacts hidden for observation n)

        Inputs:
            x: (n_obs, dim_in) tensor

        Outputs:
            jac: (n_obs, dim_out, dim_in) tensor of derivatives
        '''
        jac = []
        for n in range(x.shape[0]):
            jac_n = torch.autograd.functional.jacobian(self.hidden_features, x[n,:].reshape(1,-1)).squeeze() # dim_hidden x dim_in
            jac.append(jac_n)
        return torch.stack(jac) # n_obs x dim_out x dim_in


    def compute_Ax(self, x):
        '''
        Computes A matrix
        '''
        n = x.shape[0]
        J = self.compute_jacobian(x) # N x K x D

        #Ja = -sqrt(2/self.dim_hidden) * self.w.unsqueeze(0) * torch.sin(F.linear(x, self.w, self.b)).unsqueeze(-1) #analytical jacobian

        # all inputs
        A_d = [1/n*J[:,:,d].T@J[:,:,d] for d in range(self.dim_in)]

        # groups of inputs
        if self.groups is not None:
            A_groups = [torch.sum(torch.stack([A_d[i] for i in group]),0) for group in self.groups]
        else:
            A_groups = None

        return A_d, A_groups

    def make_unnormalized_log_prob_tf(self, x, y):

        # Set prior (since based on data)
        Ax_d, Ax_groups = self.compute_Ax(x)

        # Convert to tensors
        y_tf = tf.convert_to_tensor(y)
        h_tf = tf.convert_to_tensor(self.hidden_features(x))
        Ax_d_tf = [tf.convert_to_tensor(A) for A in Ax_d]

        if Ax_groups is not None:
            Ax_groups_tf = [tf.convert_to_tensor(A) for A in Ax_groups]

        @tf.function
        def unnormalized_log_prob(w):
            resid = y_tf - h_tf@w

            # likelihood and L2 penalty
            log_prob = -1/self.noise_sig2*tf.transpose(resid)@(resid) \
                       - tf.transpose(w)@(1/self.prior_w2_sig2*tf.eye(self.dim_hidden, dtype=tf.float64))@w 

            # Within group gradient penalty
            for scale_global, A in zip(self.scale_global, Ax_d_tf):
                grad_f_sq = tf.transpose(w)@(A)@w
                if self.penalty_type == 'l1':
                    log_prob += - scale_global*tf.math.sqrt(grad_f_sq)
                elif self.penalty_type == 'l2':
                    log_prob += - scale_global*grad_f_sq

            # Group level gradient penalty
            if Ax_groups is not None:
                for scale_groups, A in zip(self.scale_groups, Ax_groups_tf):
                    log_prob -= scale_groups*tf.math.sqrt(tf.transpose(w)@(A)@w)

            return log_prob[0,0]

        return unnormalized_log_prob


    def train(self, x, y, num_results = int(10e3), num_burnin_steps = int(1e3)):
        '''
        Train with HMC
        '''
        unnormalized_log_prob_tf = self.make_unnormalized_log_prob_tf(x, y)
        init_values = .1*np.random.randn(self.dim_hidden,1) #tf.constant(.01, shape=(self.dim_hidden,1), dtype=tf.float64) 

        samples, accept = bnn.inference.mcmc.hmc_tf(unnormalized_log_prob_tf, 
            init_values, 
            num_results, 
            num_burnin_steps, 
            num_leapfrog_steps=3, 
            step_size=1.)

        return samples, accept


class RffGradPenHyper(nn.Module):
    """
    Random features layer
    INCLUDES PRIOR ON lengthscale AND HYPERPRIOR ON prior_w2_sig2

    Variance of output layer scaled by width (see RFF activation function)

    Inputs:
    -   dim_in: dimension of inputs (int)
    -   dim_hidden: number of hidden units (int)
    -   dim_out: output dimension (int)
    -   prior_w2_sig2: prior variance of output weights. Corresponds to amplitude variance of RBF kernel. (scalar)
    -   noise_sig2: observational noise (scalar)
    -   scale_global: NOT IMPLEMENTED
    -   groups: NOT IMPLEMENTED
    -   scale_groups: NOT IMPLEMENTED
    -   lengthscale: Corresponds to lengthscale of RBF kernel. (scalar)
    -   penalty_type: select 'l1' for lasso penalty, 'l2' for ridge penalty (str)
    """
    def __init__(self, dim_in, dim_hidden, dim_out, prior_w2_sig2=1.0, noise_sig2=1.0, scale_global=1.0, groups=None, scale_groups=None, lengthscale=1.0, penalty_type='l1'):
        super(RffGradPenHyper, self).__init__()

        ### architecture
        self.dim_in = dim_in
        self.dim_hidden = dim_hidden
        self.dim_out = dim_out
        self.prior_w2_sig2 = prior_w2_sig2
        self.noise_sig2 = noise_sig2
        self.scale_global = scale_global
        self.groups = groups # list of lists with grouping (e.g. [[1,2,3], [4,5]])
        self.scale_groups = scale_groups
        self.lengthscale = lengthscale
        self.penalty_type = penalty_type

        self.register_buffer('w', torch.empty(dim_hidden, dim_in))
        self.register_buffer('b', torch.empty(dim_hidden))

        self.sample_features()

        self.act = lambda z: sqrt(2/self.dim_hidden)*torch.cos(z)
        self.act_tf = lambda z: sqrt(2/self.dim_hidden)*tf.math.cos(z)

    def sample_features(self):
        # sample random weights for RFF features
        self.w.normal_(0, 1)
        self.b.uniform_(0, 2*pi)

        self.w_tf = tf.convert_to_tensor(self.w)
        self.b_tf = tf.convert_to_tensor(self.b)
        
    def hidden_features(self, x, lengthscale=1.0):
        #return self.act(x@self.w.T + self.b.reshape(1,-1)) # (n, dim_hidden)
        return self.act(F.linear(x, self.w / lengthscale, self.b)) # (n, dim_hidden)

    def hidden_features_tf(self, x, lengthscale=1.0):
        #return self.act(x@self.w.T + self.b.reshape(1,-1)) # (n, dim_hidden)
        return self.act_tf(x @ tf.transpose(self.w_tf) / lengthscale + tf.reshape(self.b_tf, (1,-1))) # (n, dim_hidden)

    def hidden_features_tf_precompute(self, x_w_tf, lengthscale=1.0):
        return self.act_tf(x_w_tf / lengthscale + tf.reshape(self.b_tf, (1,-1))) # (n, dim_hidden)

    def make_unnormalized_log_prob_tf(self, x, y):

        # Convert to tensors
        x_tf = tf.convert_to_tensor(x)
        y_tf = tf.convert_to_tensor(y)
        n = x.shape[0]

        # for lengthscale prior and prior_w2_sig2 hyperprior
        l_alpha = tf.convert_to_tensor(1.0, dtype=tf.float64)
        l_beta = tf.convert_to_tensor(1.0, dtype=tf.float64)

        prior_w2_sig2_alpha = tf.convert_to_tensor(1.0, dtype=tf.float64)
        prior_w2_sig2_beta = tf.convert_to_tensor(1.0, dtype=tf.float64)

        def log_prob_invgamma(x, alpha, beta):
            unnormalized_prob = -(1. + alpha) * tf.math.log(x) - beta / x
            normalization = (
            tf.math.lgamma(alpha) - alpha * tf.math.log(beta))
            return unnormalized_prob - normalization

        # precompute
        x_w_tf = x @ tf.transpose(self.w_tf)

        @tf.function
        def unnormalized_log_prob(w, l, prior_w2_sig2):
            '''
            w: output layer weights
            l: lengthscale
            '''

            h_tf = self.hidden_features_tf_precompute(x_w_tf, l)
            resid = y_tf - h_tf@w

            # Jacobian of hidden layer (N x K x D)
            J = -sqrt(2/self.dim_hidden) * tf.expand_dims(self.w_tf,0) / l * tf.expand_dims(tf.math.sin(x_w_tf / l + tf.reshape(self.b_tf, (1,-1))), -1) # analytical jacobian
            
            # gradient penalties for each input dimension
            Ax_d_tf = [1/n*tf.transpose(J[:,:,d])@J[:,:,d] for d in range(self.dim_in)]

            # likelihood
            log_prob = -1/self.noise_sig2*tf.transpose(resid)@(resid)

            # L2 penalty
            log_prob += - tf.transpose(w)@(1/prior_w2_sig2*tf.eye(self.dim_hidden, dtype=tf.float64))@w 

            # prior_w2_sig2 hyperprior
            log_prob += log_prob_invgamma(prior_w2_sig2, prior_w2_sig2_alpha, prior_w2_sig2_beta)

            # lengthscale prior
            log_prob += log_prob_invgamma(l, l_alpha, l_beta)
            
            # Within group gradient penalty
            for scale_global, A in zip(self.scale_global, Ax_d_tf):
                grad_f_sq = tf.transpose(w)@(A)@w
                if self.penalty_type == 'l1':
                    log_prob += - scale_global*tf.math.sqrt(grad_f_sq)
                elif self.penalty_type == 'l2':
                    log_prob += - scale_global*grad_f_sq

            '''
            # Group level gradient penalty
            if Ax_groups is not None:
                for scale_groups, A in zip(self.scale_groups, Ax_groups_tf):
                    log_prob -= scale_groups*tf.math.sqrt(tf.transpose(w)@(A)@w)
            '''

            return log_prob[0,0]

        return unnormalized_log_prob


    def train(self, x, y, num_results = int(10e3), num_burnin_steps = int(1e3)):
        '''
        Train with HMC
        '''
        unnormalized_log_prob_tf = self.make_unnormalized_log_prob_tf(x, y)
        init_values = [.1*np.random.randn(self.dim_hidden,1), tf.constant(1.0, dtype=tf.float64), tf.constant(1.0, dtype=tf.float64)]

        samples, accept = bnn.inference.mcmc.hmc_tf(unnormalized_log_prob_tf, 
            init_values, 
            num_results, 
            num_burnin_steps, 
            num_leapfrog_steps=3, 
            step_size=1.)

        return samples, accept


        
class RffHs(nn.Module):
    """
    RFF model with horseshoe

    Currently only single layer supported
    """
    def __init__(self, 
        dim_in, \
        dim_out, \
        dim_hidden=50, \
        infer_noise=False, sig2_inv=None, sig2_inv_alpha_prior=None, sig2_inv_beta_prior=None, \
        linear_term=False, linear_dim_in=None,
        layer_in_name='RffVarSelectLogitNormalLayer',
        **kwargs):
        super(RffHs, self).__init__()
        self.dim_in = dim_in
        self.dim_out = dim_out
        self.dim_hidden = dim_hidden
        self.infer_noise=infer_noise
        self.linear_term=linear_term
        self.linear_dim_in=linear_dim_in

        # noise
        if self.infer_noise:
            self.sig2_inv_alpha_prior=torch.tensor(sig2_inv_alpha_prior)
            self.sig2_inv_beta_prior=torch.tensor(sig2_inv_beta_prior)
            self.sig2_inv = None

            self.register_buffer('sig2_inv_alpha', torch.empty(1, requires_grad=False))  # For now each output gets same noise
            self.register_buffer('sig2_inv_beta', torch.empty(1, requires_grad=False)) 
        else:
            self.sig2_inv_alpha_prior=None
            self.sig2_inv_beta_prior=None

            self.register_buffer('sig2_inv', torch.tensor(sig2_inv).clone().detach())

        # layers
        #self.layer_in = layers.RffHsLayer2(self.dim_in, self.dim_hidden, **kwargs)
        #self.layer_in = layers.RffLogitNormalLayer(self.dim_in, self.dim_hidden, **kwargs)
        self.layer_in = layers.get_layer(layer_in_name)(self.dim_in, self.dim_hidden, **kwargs)
        self.layer_out = layers.LinearLayer(self.dim_hidden, sig2_y=1/sig2_inv, **kwargs)

    def forward(self, x, x_linear=None, weights_type_layer_in='sample_post', weights_type_layer_out='sample_post', n_samp_layer_in=None):
        '''
        n_samp is number of samples from variational distribution (first layer)
        '''

        # network
        h = self.layer_in(x, weights_type=weights_type_layer_in, n_samp=n_samp_layer_in)
        y = self.layer_out(h, weights_type=weights_type_layer_out)

        # add linear term if specified
        if self.linear_term and x_linear is not None:
            return y + self.blm(x_linear, sample=sample)
        else:
            return y

    def sample_posterior_predictive(self, x_test, x_train, y_train):
        '''
        Need training data in order to get sample from non-variational full conditional distribution (output layer)

        Code duplicates some of forward, not ideal
        '''

        # 1: sample from variational distribution
        self.layer_in.sample_variational(store=True)

        # 2: forward pass of training data with sample from 1
        h = self.layer_in(x_train, weights_type='stored')

        # 3: sample output weights from conjugate (depends on ouput from 2)
        self.layer_out.fixed_point_updates(h, y_train) # conjugate update of output weights 
        self.layer_out.sample_weights(store=True)

        # 4: forward pass of test data using samples from 1 and 3
        return self.forward(x_test, weights_type_layer_in='stored', weights_type_layer_out='stored')

    def kl_divergence(self):
        return self.layer_in.kl_divergence()

    def log_prob(self, y_observed, y_pred):
        '''
        y_observed: (n_obs, dim_out)
        y_pred: (n_obs, n_pred, dim_out)

        averages over n_pred (e.g. could represent different samples), sums over n_obs
        '''
        lik = Normal(y_pred, torch.sqrt(1/self.sig2_inv))
        return lik.log_prob(y_observed.unsqueeze(1)).mean(1).sum(0)

    def loss_original(self, x, y, x_linear=None, temperature=1, n_samp=1):
        '''negative elbo'''
        y_pred = self.forward(x, x_linear, weights_type_layer_in='sample_post', weights_type_layer_out='stored', n_samp_layer_in=n_samp)

        kl_divergence = self.kl_divergence()
        #kl_divergence = 0

        log_prob = self.log_prob(y, y_pred)
        #log_prob = 0
        return -log_prob + temperature*kl_divergence

    def loss(self, x, y, x_linear=None, temperature=1, n_samp=1):
        '''
        Uses sample of weights from full conditional *based on samples of s* to compute likelihood
        '''

        kl_divergence = self.kl_divergence()
        #breakpoint()

        # 1: sample from variational distribution
        self.layer_in.sample_variational(store=True)

        # 2: forward pass of training data with sample from 1
        h = self.layer_in(x, weights_type='stored')

        # 3: sample output weights from conjugate (depends on ouput from 2)
        self.layer_out.fixed_point_updates(h, y) # conjugate update of output weights 
        self.layer_out.sample_weights(store=True)

        # 4: forward pass of test data using samples from 1 and 3
        y_pred = self.forward(x, weights_type_layer_in='stored', weights_type_layer_out='stored', n_samp_layer_in=1)

        log_prob = self.log_prob(y, y_pred)

        return -log_prob + temperature*kl_divergence


    def fixed_point_updates(self, x, y, x_linear=None, temperature=1): 
        self.layer_in.fixed_point_updates() # update horseshoe aux variables

        #### COMMENTING OUT OUTPUT LAYER UPDATES SINCE NOW PART OF LOSS FUNCTION ####
        """
        h = self.layer_in(x, weights_type='sample_post') # hidden units based on sample from variational dist
        self.layer_out.fixed_point_updates(h, y) # conjugate update of output weights 
        self.layer_out.sample_weights(store=True) # sample output weights from full conditional
        """
        ####

        if self.linear_term:
            if self.infer_noise:
                self.blm.sig2_inv = self.sig2_inv_alpha/self.sig2_inv_beta # Shouldnt this be a samplle?
            
            self.blm.fixed_point_updates(y - self.forward(x, x_linear=None, sample=True)) # Subtract off just the bnn

        if self.infer_noise and temperature > 0: 
            
            sample_y_bnn = self.forward(x, x_linear=None, sample=True) # Sample
            if self.linear_term:
                E_y_linear = F.linear(x_linear, self.blm.beta_mu)
                SSR = torch.sum((y-sample_y_bnn-E_y_linear)**2) + torch.sum(self.blm.xx_inv * self.blm.beta_sig2).sum()
            else:
                SSR = torch.sum((y - sample_y_bnn)**2)

            self.sig2_inv_alpha = self.sig2_inv_alpha_prior + temperature*0.5*x.shape[0] # Can be precomputed
            self.sig2_inv_beta = self.sig2_inv_beta_prior + temperature*0.5*SSR

    def init_parameters(self, seed=None):
        if seed is not None:
            torch.manual_seed(seed)

        self.layer_in.init_parameters()
        self.layer_out.init_parameters()

        if self.infer_noise:
            self.sig2_inv_alpha = self.sig2_inv_alpha_prior
            self.sig2_inv_beta = self.sig2_inv_beta_prior

        if self.linear_term:
            self.blm.init_parameters()

    def reinit_parameters(self, x, y, n_reinit=1):
        seeds = torch.zeros(n_reinit).long().random_(0, 1000)
        losses = torch.zeros(n_reinit)
        for i in range(n_reinit):
            self.init_parameters(seeds[i])
            losses[i] = self.loss(x, y)

        self.init_parameters(seeds[torch.argmin(losses).item()])

    def precompute(self, x=None, x_linear=None):
        # Needs to be run before training
        if self.linear_term:
            self.blm.precompute(x_linear)

    def get_n_parameters(self):
        n_param=0
        for p in self.parameters():
            n_param+=np.prod(p.shape)
        return n_param

    def print_state(self, x, y, epoch=0, n_epochs=0):
        '''
        prints things like training loss, test loss, etc
        '''
        print('Epoch[{}/{}], kl: {:.6f}, likelihood: {:.6f}, elbo: {:.6f}'\
                        .format(epoch, n_epochs, self.kl_divergence().item(), -self.loss(x,y,temperature=0).item(), -self.loss(x,y).item()))


class RffBeta(nn.Module):
    """
    RFF model beta prior on indicators

    Currently only single layer supported
    """
    def __init__(self, 
        dim_in, \
        dim_out, \
        dim_hidden=50, \
        infer_noise=False, sig2_inv=None, sig2_inv_alpha_prior=None, sig2_inv_beta_prior=None, \
        linear_term=False, linear_dim_in=None,
        **kwargs):
        super(RffBeta, self).__init__()
        self.dim_in = dim_in
        self.dim_out = dim_out
        self.dim_hidden = dim_hidden
        self.infer_noise=infer_noise
        self.linear_term=linear_term
        self.linear_dim_in=linear_dim_in

        # noise
        if self.infer_noise:
            self.sig2_inv_alpha_prior=torch.tensor(sig2_inv_alpha_prior)
            self.sig2_inv_beta_prior=torch.tensor(sig2_inv_beta_prior)
            self.sig2_inv = None

            self.register_buffer('sig2_inv_alpha', torch.empty(1, requires_grad=False))  # For now each output gets same noise
            self.register_buffer('sig2_inv_beta', torch.empty(1, requires_grad=False)) 
        else:
            self.sig2_inv_alpha_prior=None
            self.sig2_inv_beta_prior=None

            self.register_buffer('sig2_inv', torch.tensor(sig2_inv).clone().detach())

        # layers
        self.layer_in = layers.RffBetaLayer(self.dim_in, self.dim_hidden, **kwargs)
        self.layer_out = layers.LinearLayer(self.dim_hidden, sig2_y=1/sig2_inv, **kwargs)

    def forward(self, x, x_linear=None, weights_type_layer_in='sample_post', weights_type_layer_out='sample_post'):

        # network
        h = self.layer_in(x, weights_type=weights_type_layer_in)
        y = self.layer_out(h, weights_type=weights_type_layer_out)

        # add linear term if specified
        if self.linear_term and x_linear is not None:
            return y + self.blm(x_linear, sample=sample)
        else:
            return y

    def kl_divergence(self):
        return self.layer_in.kl_divergence()

    def compute_loss_gradients(self, x, y, x_linear=None, temperature=1.):

        # sample from variational dist
        self.layer_in.sample_variational(store=True)

        # compute log likelihood
        y_pred = self.forward(x, x_linear, weights_type_layer_in='stored', weights_type_layer_out='stored')
        log_lik = -self.neg_log_prob(y, y_pred)

        # gradients of score function
        for p in self.layer_in.parameters(): 
            if p.grad is not None:
                p.grad.zero_()

        log_q = self.layer_in.log_prob_variational()
        log_q.backward()

        self.layer_in.s_a_trans_grad_q = self.layer_in.s_a_trans.grad.clone()
        self.layer_in.s_b_trans_grad_q = self.layer_in.s_b_trans.grad.clone()

        # gradients of kl
        for p in self.layer_in.parameters(): p.grad.zero_()

        kl = self.kl_divergence()
        kl.backward()

        self.layer_in.s_a_trans_grad_kl = self.layer_in.s_a_trans.grad.clone()
        self.layer_in.s_b_trans_grad_kl = self.layer_in.s_b_trans.grad.clone()

        # gradients of loss=-elbo
        with torch.no_grad():
            self.layer_in.s_a_trans.grad = -log_lik*self.layer_in.s_a_trans_grad_q + temperature*self.layer_in.s_a_trans_grad_kl
            self.layer_in.s_b_trans.grad = -log_lik*self.layer_in.s_b_trans_grad_q + temperature*self.layer_in.s_b_trans_grad_kl

    def loss(self, x, y, x_linear=None, temperature=1):
        '''negative elbo
        NON DIFFERENTIABLE BECAUSE OF SCORE METHOD
        '''
        y_pred = self.forward(x, x_linear, weights_type_layer_in='sample_post', weights_type_layer_out='stored')

        kl_divergence = self.kl_divergence()
        #kl_divergence = 0

        neg_log_prob = self.neg_log_prob(y, y_pred)
        #neg_log_prob = 0

        return neg_log_prob + temperature*kl_divergence

    def neg_log_prob(self, y_observed, y_pred):
        N = y_observed.shape[0]
        if self.infer_noise:
            sig2_inv = self.sig2_inv_alpha/self.sig2_inv_beta # Is this right? i.e. IG vs G
        else:
            sig2_inv = self.sig2_inv
        log_prob = -0.5 * N * math.log(2 * math.pi) + 0.5 * N * torch.log(sig2_inv) - 0.5 * torch.sum((y_observed - y_pred)**2) * sig2_inv
        return -log_prob

    def fixed_point_updates(self, x, y, x_linear=None, temperature=1): 

        h = self.layer_in(x, weights_type='sample_post') # hidden units based on sample from variational dist
        self.layer_out.fixed_point_updates(h, y) # conjugate update of output weights 

        self.layer_out.sample_weights(store=True) # sample output weights from full conditional

        if self.linear_term:
            if self.infer_noise:
                self.blm.sig2_inv = self.sig2_inv_alpha/self.sig2_inv_beta # Shouldnt this be a samplle?
            
            self.blm.fixed_point_updates(y - self.forward(x, x_linear=None, sample=True)) # Subtract off just the bnn

        if self.infer_noise and temperature > 0: 
            
            sample_y_bnn = self.forward(x, x_linear=None, sample=True) # Sample
            if self.linear_term:
                E_y_linear = F.linear(x_linear, self.blm.beta_mu)
                SSR = torch.sum((y-sample_y_bnn-E_y_linear)**2) + torch.sum(self.blm.xx_inv * self.blm.beta_sig2).sum()
            else:
                SSR = torch.sum((y - sample_y_bnn)**2)

            self.sig2_inv_alpha = self.sig2_inv_alpha_prior + temperature*0.5*x.shape[0] # Can be precomputed
            self.sig2_inv_beta = self.sig2_inv_beta_prior + temperature*0.5*SSR

    def init_parameters(self, seed=None):
        if seed is not None:
            torch.manual_seed(seed)

        self.layer_in.init_parameters()
        self.layer_out.init_parameters()

        if self.infer_noise:
            self.sig2_inv_alpha = self.sig2_inv_alpha_prior
            self.sig2_inv_beta = self.sig2_inv_beta_prior

        if self.linear_term:
            self.blm.init_parameters()

    def reinit_parameters(self, x, y, n_reinit=1):
        seeds = torch.zeros(n_reinit).long().random_(0, 1000)
        losses = torch.zeros(n_reinit)
        for i in range(n_reinit):
            self.init_parameters(seeds[i])
            losses[i] = self.loss(x, y)

        self.init_parameters(seeds[torch.argmin(losses).item()])

    def precompute(self, x=None, x_linear=None):
        # Needs to be run before training
        if self.linear_term:
            self.blm.precompute(x_linear)

    def get_n_parameters(self):
        n_param=0
        for p in self.parameters():
            n_param+=np.prod(p.shape)
        return n_param

    def print_state(self, x, y, epoch=0, n_epochs=0):
        '''
        prints things like training loss, test loss, etc
        '''
        print('Epoch[{}/{}], kl: {:.6f}, likelihood: {:.6f}, elbo: {:.6f}'\
                        .format(epoch, n_epochs, self.kl_divergence().item(), -self.loss(x,y,temperature=0).item(), -self.loss(x,y).item()))




def train(model, optimizer, x, y, n_epochs, x_linear=None, n_warmup = 0, n_rep_opt=10, print_freq=None, frac_start_save=1, frac_lookback=0.5, path_checkpoint='./'):
    '''
    frac_lookback will only result in reloading early stopped model if frac_lookback < 1 - frac_start_save
    '''

    loss = torch.zeros(n_epochs)
    loss_best = torch.tensor(float('inf'))
    loss_best_saved = torch.tensor(float('inf'))
    saved_model = False
    model.precompute(x, x_linear)

    for epoch in range(n_epochs):

        # TEMPERATURE HARDECODED, NEED TO FIX
        #temperature_kl = 0. if epoch < n_epochs/2 else 1.0
        #temperature_kl = epoch / (n_epochs/2) if epoch < n_epochs/2 else 1.0
        temperature_kl = epoch / (n_epochs/10) if epoch < n_epochs/10 else 1.0
        #temperature_kl = 0. # SET TO ZERO TO IGNORE KL

        for i in range(n_rep_opt):

            l = model.loss(x, y, x_linear=x_linear, temperature=temperature_kl)

            # backward
            optimizer.zero_grad()
            l.backward(retain_graph=True)
            optimizer.step()

            ##
            #print('------------- %d -------------' % epoch)
            #print('s     :', model.layer_in.s_loc.data)
            #print('grad  :', model.layer_in.s_loc.grad)

            #model.layer_in.s_loc.grad.zero_()
            #kl = model.layer_in.kl_divergence()
            #kl.backward()
            #if epoch > 500:
            #    breakpoint()
            #print('grad kl:', model.layer_in.s_loc.grad)
            ##

        loss[epoch] = l.item()

        with torch.no_grad():
            model.fixed_point_updates(x, y, x_linear=x_linear, temperature=1)

        # print state
        if print_freq is not None:
            if (epoch + 1) % print_freq == 0:
                model.print_state(x, y, epoch+1, n_epochs)

        # see if improvement made (only used if KL isn't tempered)
        if loss[epoch] < loss_best and temperature_kl==1.0:
            loss_best = loss[epoch]

        # save model
        if epoch > frac_start_save*n_epochs and loss[epoch] < loss_best_saved:
            print('saving mode at epoch = %d' % epoch)
            saved_model = True
            loss_best_saved = loss[epoch]
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss[epoch],
            },  os.path.join(path_checkpoint, 'checkpoint.tar'))

        # end training if no improvement made in a while and more than half way done
        epoch_lookback = np.maximum(1, int(epoch - .25*n_epochs)) # lookback is 25% of samples by default
        if epoch_lookback > frac_start_save*n_epochs+1:
            loss_best_lookback = torch.min(loss[epoch_lookback:epoch+1])
            percent_improvement = (loss_best - loss_best_lookback)/torch.abs(loss_best) # positive is better
            if percent_improvement < 0.0:
                print('stopping early at epoch = %d' % epoch)
                break

    # reload best model if saving
    if saved_model:
        checkpoint = torch.load(os.path.join(path_checkpoint, 'checkpoint.tar'))
        model.load_state_dict(checkpoint['model_state_dict'])
        print('reloading best model from epoch = %d' % checkpoint['epoch'])
        model.eval()


    return loss[:epoch]


def train_score(model, optimizer, x, y, n_epochs, x_linear=None, n_warmup = 0, n_rep_opt=10, print_freq=None, frac_start_save=1):
    loss = torch.zeros(n_epochs)
    loss_best = 1e9 # Need better way of initializing to make sure it's big enough
    model.precompute(x, x_linear)

    for epoch in range(n_epochs):

        # TEMPERATURE HARDECODED, NEED TO FIX
        #temperature_kl = 0. if epoch < n_epochs/2 else 1
        #temperature_kl = epoch / (n_epochs/2) if epoch < n_epochs/2 else 1
        temperature_kl = 0. # SET TO ZERO TO IGNORE KL

        for i in range(n_rep_opt):

            optimizer.zero_grad()

            model.compute_loss_gradients(x, y, x_linear=x_linear, temperature=temperature_kl)

            # backward
            torch.nn.utils.clip_grad_norm_(model.parameters(), 100)
            optimizer.step()

        with torch.no_grad():
            model.fixed_point_updates(x, y, x_linear=x_linear, temperature=1)

        if epoch > frac_start_save*n_epochs and loss[epoch] < loss_best: 
            print('saving...')
            loss_best = loss[epoch]
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss[epoch],
            }, 'checkpoint.tar')

        if print_freq is not None:
            if (epoch + 1) % print_freq == 0:
                model.print_state(x, y, epoch+1, n_epochs)

    return loss
