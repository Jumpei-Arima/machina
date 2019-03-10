import numpy as np
import torch

from machina.pds import DeterministicPd
from machina.pols import BasePol
from machina.utils import get_device


class PlanetPol(BasePol):
    """
    Policy with cross entropy method of planet.

    Parameters
    ----------
    ob_space : gym.Space
        observation's space
    ac_space : gym.Space
        action's space.
        This should be gym.spaces.Box
    net : torch.nn.Module
        dymamics model
    rew_func : function
        rt = rew_func(st+1, at). rt, st+1 and at are torch.tensor.
    n_samples : int
        num of action samples in the model predictive control
    horizon : int
        horizon of prediction
    mean_obs : np.array
    std_obs : np.array
    mean_acs : np.array
    std_acs : np.array
    rnn : bool
    normalize_ac : bool
        If True, the output of network is spreaded for ac_space.
        In this situation the output of network is expected to be in -1~1.
    data_parallel : bool
        If True, network computation is executed in parallel.
    parallel_dim : int
        Splitted dimension in data parallel.
    """

    def __init__(self, ob_space, ac_space, rssm, rew_model, horizon=12, n_optim_iters=10, n_samples=1000,
                 n_refit_samples=100, data_parallel=False, parallel_dim=0):
        BasePol.__init__(self, ob_space, ac_space, net=None, rnn=True, normalize_ac=True,
                         data_parallel=data_parallel, parallel_dim=parallel_dim)
        self.rssm = rssm
        self.rew_model = rew_model
        self.horizon = horizon
        self.n_optim_iters = n_optim_iters
        self.n_samples = n_samples
        self.n_refit_samples = n_refit_samples
        self.prev_state = torch.zeros(
            1, self.rssm.state_size, dtype=torch.float)
        self.prev_acs = torch.zeros(
            1, self.ac_space.shape[0], dtype=torch.float)
        self.hs = torch.zeros(
            1, self.rssm.belief_size, dtype=torch.float)
        self.to(get_device())

    def to(self, device):
        super().to(device)
        self.rssm.to(device)

    def reset(self):
        super(PlanetPol, self).reset()
        self.rssm.reset()
        self.prev_state = torch.zeros(
            1, self.rssm.state_size, dtype=torch.float)
        self.prev_acs = torch.zeros(
            1, self.ac_space.shape[0], dtype=torch.float)
        self.hs = torch.zeros(
            1, self.rssm.belief_size, dtype=torch.float)

    def forward(self, obs, hs=None, h_masks=None):
        # initialize action distribution
        mean = torch.zeros(
            self.horizon, self.ac_space.shape[0], dtype=torch.float)
        std = torch.ones(
            self.horizon, self.ac_space.shape[0], dtype=torch.float)
        obs = obs.unsqueeze(0)
        embedded_obs = self.rssm.encode(obs).repeat(self.n_samples, 1)

        with torch.no_grad():
            self.n_optim_iters = 1
            self.n_refit_samples = 1
            for iters in range(self.n_optim_iters):
                sum_rews = 0
                prev_state = self.prev_state.repeat(self.n_samples, 1)
                prev_acs = self.prev_acs.repeat(self.n_samples, 1)
                hs = self.hs.repeat(self.n_samples, 1)

                # randomly sample N candidate action sequences
                # candidate_acs = torch.randn(
                #    self.horizon, self.n_samples, self.ac_space.shape[0]) * std.unsqueeze(1) + mean.unsqueeze(1)
                candidate_acs = torch.empty(self.horizon, self.n_samples, self.ac_space.shape[0], dtype=torch.float).uniform_(
                    self.ac_space.low[0], self.ac_space.high[0])
                for i in range(candidate_acs.size()[-1]):
                    candidate_acs[:, :, i] = candidate_acs[:, :, i].clamp(
                        min=self.ac_space.low[i], max=self.ac_space.high[i])

                # Evaluate action sequences frin the current belief
                posterior_state = self.rssm.posterior(
                    prev_state, prev_acs, embedded_obs, hs)
                prev_state = posterior_state['sample']
                hs = posterior_state['belief']
                for acs in candidate_acs:
                    prior_state = self.rssm.prior(prev_state, acs, hs)
                    features = self.rssm.features_from_state(prior_state)
                    rews, _ = self.rew_model(features, acs=None)
                    sum_rews += rews
                    prev_state = prior_state['sample']
                    hs = prior_state['belief']

                # re-fit belief to the K(n_refit_samples) best action sequence
                k_best_acs_indices = np.argsort(
                    rews.squeeze(-1).cpu().numpy())[::-1][0:self.n_refit_samples]
                k_best_acs_indices = torch.from_numpy(
                    k_best_acs_indices.copy())
                mean = torch.mean(
                    candidate_acs[:, k_best_acs_indices, :], dim=1, keepdim=True)
                std = torch.sum(torch.abs(
                    candidate_acs[:, k_best_acs_indices, :] - mean), dim=1) / (self.n_refit_samples - 1)
                mean = mean.squeeze(1)
                std = std.squeeze(1)

        ac = mean[0]
        ac_real = ac.cpu().numpy()

        # Actual latent transition
        prior_state = self.rssm.prior(
            self.prev_state, ac.unsqueeze(0), self.hs)
        self.prev_state = prior_state['sample']
        self.prev_acs = ac.unsqueeze(0)
        self.hs = prior_state['belief']

        return ac_real, ac, dict(mean=ac)

    def deterministic_ac_real(self, obs):
        """
        action for deployment
        """
        mean_read, mean, dic = self.forward(obs)
        return mean_real, mean, dic