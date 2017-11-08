from __future__ import division
import sys
import os
import numpy as np
from math import log
from collections import deque
from random import random,randrange
from operator import attrgetter
from scipy.interpolate import UnivariateSpline

from . import parameter
from . import proposal

class Sampler(object):
    """
    Sampler class.
    Initialisation arguments:
    
    usermodel:
    user defined model to sample
    
    maxmcmc:
    maximum number of mcmc steps to be used in the sampler
    
    verbose:
    display debug information on screen
    default: False
    
    poolsize:
    number of objects for the affine invariant sampling
    default: 1000
    """
    def __init__(self,usermodel,maxmcmc,verbose=False,poolsize=1000):
        self.user = usermodel
        self.maxmcmc = maxmcmc
        self.Nmcmc = 100
        self.Nmcmc_exact = float(maxmcmc)
        self.proposals = proposal.DefaultProposalCycle()
        self.poolsize = poolsize
        self.evolution_points = deque(maxlen=self.poolsize + 1) # +1 for the point to evolve
        self.verbose=verbose
        self.acceptance=0.0
        self.initialised=False
        
    def reset(self):
        """
        Initialise the sampler
        """
        for n in range(self.poolsize):
          while True:
            if self.verbose > 2: sys.stderr.write("process {0!s} --> generating pool of {1:d} points for evolution --> {2:.0f} % complete\r".format(os.getpid(), self.poolsize, 100.0*float(n+1)/float(self.poolsize)))
            p = self.user.new_point()
            p.logP = self.user.log_prior(p)
            if np.isfinite(p.logP): break
          p.logL=self.user.log_likelihood(p)
          self.evolution_points.append(p)
        if self.verbose > 2: sys.stderr.write("\n")
        self.proposals.set_ensemble(self.evolution_points)
        for _ in range(len(self.evolution_points)):
          s = self.evolution_points.popleft()
          s = self.metropolis_hastings(s,-np.inf)
          self.evolution_points.append(s)
        self.proposals.set_ensemble(self.evolution_points)
        self.initialised=True

    def estimate_nmcmc(self, safety=5, tau=None):
        """
        Estimate autocorrelation length of chain using acceptance fraction
        ACL = (2/acc) - 1
        multiplied by a safety margin of 5
        Uses moving average with decay time tau iterations (default: self.poolsize)
        Taken from W. Farr's github.com/farr/Ensemble.jl
        """
        if tau is None: tau = self.poolsize

        if self.acceptance == 0.0:
            self.Nmcmc_exact = (1.0 + 1.0/tau)*self.Nmcmc_exact
        else:
            self.Nmcmc_exact = (1.0 - 1.0/tau)*self.Nmcmc_exact + (safety/tau)*(2.0/self.acceptance - 1.0)
        
        self.Nmcmc_exact = float(min(self.Nmcmc_exact,self.maxmcmc))
        self.Nmcmc = max(safety,int(self.Nmcmc_exact))
        return self.Nmcmc

    def produce_sample(self, queue, logLmin, seed, ip, port, authkey):
        """
        main loop that generates samples and puts them in the queue for the nested sampler object
        """
        if not self.initialised:
          self.reset()
        # Prevent process from zombification if consumer thread exits
        queue.cancel_join_thread()
        self.seed = seed
        np.random.seed(seed=self.seed)
        self.counter=0
        
        while(1):
            
            # Pick a the first point from the ensemble to start with
            # Pop it out the stack to prevent cloning

            param = self.evolution_points.popleft()
            
            if logLmin.value==np.inf:
                break
            
            outParam = self.metropolis_hastings(param,logLmin.value)
           
            # Put sample back in the stack
            self.evolution_points.append(outParam.copy())
            # If we bailed out then flag point as unusable
            if self.acceptance==0.0:
                outParam.logL=-np.inf
            # Push the sample onto the queue
            queue.put((self.acceptance,self.jumps,outParam))
            # Update the ensemble every now and again
            if (self.counter%(self.poolsize/10))==0 or self.acceptance == 0.0:
                self.proposals.set_ensemble(self.evolution_points)
            self.counter += 1

        sys.stderr.write("Sampler process {0!s}, exiting\n".format(os.getpid()))
        return 0

    def metropolis_hastings(self,inParam,logLmin):
        """
        metropolis-hastings loop to generate the new live point taking nmcmc steps
        """
        self.jumps = 0
        accepted = 0
        oldparam = inParam.copy()
        logp_old = self.user.log_prior(oldparam)
        
        while self.jumps < self.Nmcmc:
            
            newparam = self.proposals.get_sample(oldparam.copy())
            newparam.logP = self.user.log_prior(newparam)
            
            if newparam.logP-logp_old + self.proposals.log_J > log(random()):
                newparam.logL = self.user.log_likelihood(newparam)
                
                if newparam.logL > logLmin:
                    oldparam = newparam
                    logp_old = newparam.logP
                    accepted+=1
                        
            self.jumps+=1
            
            if self.jumps > self.maxmcmc: break

        self.acceptance = float(accepted)/float(self.jumps)
        self.estimate_nmcmc()
        return oldparam

class HMCSampler(object):
    """
    Hamiltonian Monte Carlo Sampler class.
    Initialisation arguments:
    
    usermodel:
    user defined model to sample
    
    maxmcmc:
    maximum number of mcmc steps to be used in the sampler
    
    verbose:
    display debug information on screen
    default: False
    
    poolsize:
    number of objects for the affine invariant sampling
    default: 1000
    """
    def __init__(self,usermodel,maxmcmc,verbose=False,poolsize=1000):
        self.user = usermodel
        self.maxmcmc = maxmcmc
        self.Nmcmc = 100
        self.Nmcmc_exact = float(maxmcmc)
        self.proposals = proposal.DefaultProposalCycle()
        self.poolsize = poolsize
        self.evolution_points = deque(maxlen=self.poolsize + 1) # +1 for the point to evolve
        self.verbose=verbose
        self.acceptance=0.0
        self.initialised=False
        self.gradient = []
        self.step_size = 0.1
        self.steps = 20
    
    def reset(self):
        """
        Initialise the sampler
        """
        for n in range(self.poolsize):
          while True:
            if self.verbose > 2: sys.stderr.write("process {0!s} --> generating pool of {1:d} points for evolution --> {2:.0f} % complete\r".format(os.getpid(), self.poolsize, 100.0*float(n+1)/float(self.poolsize)))
            p = self.user.new_point()
            p.logP = self.user.log_prior(p)
            if np.isfinite(p.logP): break
          p.logL=self.user.log_likelihood(p)
          self.evolution_points.append(p)
        if self.verbose > 2: sys.stderr.write("\n")
        self.proposals.set_ensemble(self.evolution_points)
        for _ in range(len(self.evolution_points)):
          s = self.evolution_points.popleft()
          s = self.metropolis_hastings(s,-np.inf)
          self.evolution_points.append(s)
        self.proposals.set_ensemble(self.evolution_points)
        self.initialised=True

    def estimate_nmcmc(self, safety=5, tau=None):
        """
        Estimate autocorrelation length of chain using acceptance fraction
        ACL = (2/acc) - 1
        multiplied by a safety margin of 5
        Uses moving average with decay time tau iterations (default: self.poolsize)
        Taken from W. Farr's github.com/farr/Ensemble.jl
        """
        if tau is None: tau = self.poolsize

        if self.acceptance == 0.0:
            self.Nmcmc_exact = (1.0 + 1.0/tau)*self.Nmcmc_exact
        else:
            self.Nmcmc_exact = (1.0 - 1.0/tau)*self.Nmcmc_exact + (safety/tau)*(2.0/self.acceptance - 1.0)
        
        self.Nmcmc_exact = float(min(self.Nmcmc_exact,self.maxmcmc))
        self.Nmcmc = max(safety,int(self.Nmcmc_exact))
        return self.Nmcmc

    def produce_sample(self, queue, logLmin, seed, ip, port, authkey):
        """
        main loop that generates samples and puts them in the queue for the nested sampler object
        """
        if not self.initialised:
          self.reset()
        # Prevent process from zombification if consumer thread exits
        queue.cancel_join_thread()
        self.seed = seed
        np.random.seed(seed=self.seed)
        self.counter=0
        
        while(1):
            
            # Pick a the first point from the ensemble to start with
            # Pop it out the stack to prevent cloning

            param = self.evolution_points.popleft()
            
            if logLmin.value==np.inf:
                break
            
            outParam = self.hamiltonian_sampling(param,logLmin.value)
           
            # Put sample back in the stack
            self.evolution_points.append(outParam.copy())
            # If we bailed out then flag point as unusable
            if self.acceptance==0.0:
                outParam.logL=-np.inf
            # Push the sample onto the queue
            queue.put((self.acceptance,self.jumps,outParam))
            # Update the ensemble every now and again
            if (self.counter%(self.poolsize/10))==0 or self.acceptance == 0.0:
                self.proposals.set_ensemble(self.evolution_points)
                self.estimate_gradient()
            
            self.counter += 1

        sys.stderr.write("Sampler process {0!s}, exiting\n".format(os.getpid()))
        return 0

    def metropolis_hastings(self,inParam,logLmin):
        """
        metropolis-hastings loop to generate the new live point taking nmcmc steps
        """
        self.jumps = 0
        accepted = 0
        oldparam = inParam.copy()
        logp_old = self.user.log_prior(oldparam)
        
        while self.jumps < self.Nmcmc:
            
            newparam = self.proposals.get_sample(oldparam.copy())
            newparam.logP = self.user.log_prior(newparam)
            
            if newparam.logP-logp_old + self.proposals.log_J > log(random()):
                newparam.logL = self.user.log_likelihood(newparam)
                
                if newparam.logL > logLmin:
                    oldparam = newparam
                    logp_old = newparam.logP
                    accepted+=1
            
            self.jumps+=1
            
            if self.jumps > self.maxmcmc: break

        self.acceptance = float(accepted)/float(self.jumps)
        self.estimate_nmcmc()
        return oldparam

    def estimate_gradient(self):
        
        self.gradients = []
        logLs = [p.logL for p in self.evolution_points]
        # loop over the parameters, spline interpolate and estimate the gradient
        
        for key in self.evolution_points[0].names:
            x = [self.evolution_points[i][key] for i in range(len(self.evolution_points))]
            idx = np.argsort(x)
            x = x[idx]
            logLs = logL[idx]
            self.gradients.append(UnivariateSpline(x,logLs,ext=0).derivative())

    def gradient(self, inParam):
        return np.array([g(x) for g,x in zip(self.gradients,self.evolution_points[0].names)])
    
    def kinetic_energy(self):
        """Kinetic energy of the current velocity (assuming a standard Gaussian)
            (x dot x) / 2
        Parameters
        ----------
        velocity : tf.Variable
            Vector of current velocity
        Returns
        -------
        kinetic_energy : float
        """
        return 0.5 * self.velocity * self.velocity

    def hamiltonian(self, inParam):
        """Computes the Hamiltonian of the current position, velocity pair
        H = U(x) + K(v)
        U is the potential energy and is = -log_posterior(x)
        Parameters
        ----------
        position : tf.Variable
            Position or state vector x (sample from the target distribution)
        velocity : tf.Variable
            Auxiliary velocity variable
        energy_function
            Function from state to position to 'energy'
             = -log_posterior
        Returns
        -------
        hamitonian : float
        """
        return -self.user.log_likelihood(newparam) + kinetic_energy()

    def leapfrog_step(self, inParam):

        # Start by updating the velocity a half-step
        self.velocity += - 0.5 * self.step_size * self.gradient(inParam)

        # Initalize x to be the first step
        p = inParam + self.step_size * self.velocity

        for i in xrange(self.steps):

            # Update velocity
            self.velocity += - self.step_size * self.gradient(p)

            # Update x
            p += self.step_size * self.velocity

        # Do a final update of the velocity for a half step
        self.velocity += - 0.5 * self.step_size * self.gradient(p)
        
        return p
        
    def hamiltonian_sampling(self,inParam,logLmin):
        """
        metropolis-hastings loop to generate the new live point taking nmcmc steps
        """
        self.jumps = 0
        accepted = 0
        oldparam = inParam.copy()
        orig = self.hamiltonian(oldparam)
        
        while self.jumps < self.Nmcmc:
            
            newparam = self.leapfrog_step(oldparam.copy())
            newparam.logP = self.user.log_prior(newparam)
            newparam.logL = self.user.log_likelihood(newparam)
            
            current = hamiltonian(newparam)
            p_accept = min(1.0, np.exp(orig - current))
    
            if p_accept > np.random.uniform():

                if newparam.logL > logLmin:
                    oldparam = newparam
                    logp_old = newparam.logP
                    accepted+=1
            
            self.jumps+=1
            
            if self.jumps > self.maxmcmc: break

        self.acceptance = float(accepted)/float(self.jumps)
        self.estimate_nmcmc()
        return oldparam
