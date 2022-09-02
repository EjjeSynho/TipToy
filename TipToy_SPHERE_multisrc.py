#%%
import torch
from torch import nn, optim, fft
from torch.nn import ParameterDict, ParameterList, Parameter
from torch.nn.functional import interpolate

from tqdm import tqdm
import numpy as np
import matplotlib.pyplot as plt
import scipy.special as spc
from scipy import signal
from astropy.io import fits
import pickle
import time
import re
import os
from os import path
from copy import deepcopy

from parameterParser import parameterParser
from SPHERE_data import SPHERE_database,LoadSPHEREsampleByID
from utils import rad2mas, rad2arc, deg2rad, asec2rad, seeing, r0, r0_new
from utils import Center, BackgroundEstimate, CircularMask
from utils import register_hooks, iter_graph
from utils import OptimizeTRF, OptimizeLBFGS
from utils import radial_profile, plot_radial_profile
# end of import list

#%%

#210
#13
#190
#26
#422
#102


data_samples = []
data_samples.append( LoadSPHEREsampleByID('C:/Users/akuznets/Data/SPHERE/test/', 260)[1] )
data_samples.append( LoadSPHEREsampleByID('C:/Users/akuznets/Data/SPHERE/test/', 450)[1] )
data_samples.append( LoadSPHEREsampleByID('C:/Users/akuznets/Data/SPHERE/test/', 26 )[1] )


path_root = path.normpath('C:/Users/akuznets/Projects/TIPTOP_old/P3')
path_ini = path.join(path_root, path.normpath('aoSystem/parFiles/irdis.ini'))

config_file = parameterParser(path_root, path_ini).params


#%% ------------------------ Managing paths ------------------------
class TipToy(torch.nn.Module):

    def SetDataSample(self, data_samples):
        # Reading parameters from the file
        num_src = len(data_samples)

        self.wvl = torch.tensor([data_sample['spectrum']['lambda'] for data_sample in data_samples], device=self.device)
        self.configs = deepcopy(self.AO_config)
        self.configs['sources_science']['Wavelength'] = self.wvl
        self.configs['sensor_science']['FieldOfView'] = data_samples[0]['image'].shape[0] #TODO: image size check

        self.configs['atmosphere']['Seeing']        = torch.tensor([data_sample['seeing']['SPARTA'] for data_sample in data_samples], device=self.device)
        self.configs['atmosphere']['WindSpeed']     = torch.tensor([data_sample['Wind speed']['header'] for data_sample in data_samples], device=self.device)
        self.configs['atmosphere']['WindDirection'] = torch.tensor([data_sample['Wind direction']['header'] for data_sample in data_samples], device=self.device)
        self.configs['sensor_science']['Zenith']    = torch.tensor([90.0-data_sample['telescope']['altitude'] for data_sample in data_samples], device=self.device)
        self.configs['telescope']['ZenithAngle']    = torch.tensor([90.0-data_sample['telescope']['altitude'] for data_sample in data_samples], device=self.device)
        self.configs['sensor_science']['Azimuth']   = torch.tensor([data_sample['telescope']['azimuth'] for data_sample in data_samples], device=self.device)
        self.configs['sensor_science']['SigmaRON']  = torch.tensor([data_sample['Detector']['ron']  for data_sample in data_samples], device=self.device)
        self.configs['sensor_science']['Gain']      = torch.tensor([data_sample['Detector']['gain'] for data_sample in data_samples], device=self.device)
        self.configs['sensor_HO']['NumberPhotons']  = torch.tensor([data_sample['WFS']['Nph vis'] for data_sample in data_samples], device=self.device)
        self.configs['sources_HO']['Wavelength']    = torch.tensor([self.AO_config['sources_HO']['Wavelength'][0] for _ in range(num_src)], device=self.device)
        self.configs['sources_HO']['Height']        = torch.tensor([self.AO_config['sources_HO']['Height']  for _ in range(num_src)], device=self.device)
        self.configs['sources_HO']['Zenith']        = torch.tensor([self.AO_config['sources_HO']['Zenith']  for _ in range(num_src)], device=self.device)
        self.configs['sources_HO']['Azimuth']       = torch.tensor([self.AO_config['sources_HO']['Azimuth'] for _ in range(num_src)], device=self.device)
        self.configs['atmosphere']['Cn2Weights']    = torch.tensor([self.AO_config['atmosphere']['Cn2Weights'] for _ in range(num_src)], device=self.device)
        self.configs['atmosphere']['Cn2Heights']    = torch.tensor([self.AO_config['atmosphere']['Cn2Heights'] for _ in range(num_src)], device=self.device)
        self.configs['RTC']['LoopDelaySteps_HO']    = torch.tensor([self.AO_config['RTC']['LoopDelaySteps_HO'] for _ in range(num_src)], device=self.device)
        self.configs['RTC']['LoopGain_HO']          = torch.tensor([self.AO_config['RTC']['LoopGain_HO']   for _ in range(num_src)], device=self.device)
        self.configs['RTC']['SensorFrameRate_HO']   = torch.tensor([data_sample['WFS']['rate'] for data_sample in data_samples], device=self.device)

        # Setting internal parameters
        self.D       = self.AO_config['telescope']['TelescopeDiameter']
        self.psInMas = self.AO_config['sensor_science']['PixelScale'] #[mas]
        self.nPix    = self.AO_config['sensor_science']['FieldOfView']
        self.pitch   = self.AO_config['DM']['DmPitchs'][0] #[m]
        #self.h_DM    = self.AO_config['DM']['DmHeights'][0] # ????? what is h_DM?
        #self.nDM     = 1
        self.kc      = 1/(2*self.pitch) #TODO: kc is not consistent with vanilla TIPTOP

        #self.zenith_angle  = torch.tensor(self.AO_config['telescope']['ZenithAngle'], device=self.device) # [deg] #TODO: telescope zenith != sample zenith?
        self.zenith_angle  = self.configs['telescope']['ZenithAngle']
        self.airmass       = 1.0 / torch.cos(self.zenith_angle * deg2rad)

        self.GS_wvl     = self.AO_config['sources_HO']['Wavelength'][0] #[m]
        #self.GS_height  = self.AO_config['sources_HO']['Height'] * self.airmass #[m]

        self.wind_speed  = self.configs['atmosphere']['WindSpeed']
        self.wind_dir    = self.configs['atmosphere']['WindDirection']
        self.Cn2_weights = self.configs['atmosphere']['Cn2Weights']
        self.Cn2_heights = self.configs['atmosphere']['Cn2Heights'] * self.airmass #[m]
        #self.stretch     = 1.0 / (1.0-self.Cn2_heights/self.GS_height)
        self.h           = self.Cn2_heights #* self.stretch
        self.nL          = self.Cn2_heights.size(0)

        self.WFS_d_sub = np.mean(self.AO_config['sensor_HO']['SizeLenslets'])
        self.WFS_n_sub = np.mean(self.AO_config['sensor_HO']['NumberLenslets'])

        self.WFS_det_clock_rate = np.mean(self.AO_config['sensor_HO']['ClockRate']) # [(?)]
        self.WFS_FOV = self.AO_config['sensor_HO']['FieldOfView']
        self.WFS_RON = self.AO_config['sensor_HO']['SigmaRON']
        self.WFS_psInMas = self.AO_config['sensor_HO']['PixelScale']
        self.WFS_wvl = torch.tensor(self.GS_wvl, device=self.device) #TODO: clarify this
        self.WFS_spot_FWHM = self.configs['sensor_HO']['SpotFWHM'][0]
        self.WFS_excessive_factor = self.configs['sensor_HO']['ExcessNoiseFactor']
        self.WFS_Nph = self.configs['sensor_HO']['NumberPhotons']

        self.HOloop_rate  = np.mean(self.AO_config['RTC']['SensorFrameRate_HO']) # [Hz] (?)
        self.HOloop_delay = self.AO_config['RTC']['LoopDelaySteps_HO'] # [ms] (?)
        self.HOloop_gain  = self.AO_config['RTC']['LoopGain_HO']


    def InitGrids(self):
        if self.pixels_per_l_D is None:
            self.pixels_per_l_D = self.wvl*rad2mas / (self.psInMas*self.D)

        self.sampling_factor = (torch.ceil(2.0/self.pixels_per_l_D)).int() # check how much it is less than Nyquist
        self.sampling = self.sampling_factor * self.pixels_per_l_D
        self.nOtf = self.nPix * self.sampling_factor.max().item()

        self.dk = 1/self.D/self.sampling.min() # PSD spatial frequency step
        self.cte = (24*spc.gamma(6/5)/5)**(5/6)*(spc.gamma(11/6)**2/(2*np.pi**(11/3)))

        # Initialize spatial frequencies
        self.kx, self.ky = torch.meshgrid(
            torch.linspace(-self.nOtf/2, self.nOtf/2-1, self.nOtf, device=self.device)*self.dk + 1e-10,
            torch.linspace(-self.nOtf/2, self.nOtf/2-1, self.nOtf, device=self.device)*self.dk + 1e-10,
            indexing = 'ij')

        self.k2 = self.kx**2 + self.ky**2
        self.k = torch.sqrt(self.k2)

        self.mask = torch.ones_like(self.k2, device=self.device)
        self.mask[self.k2 <= self.kc**2] = 0
        self.mask_corrected = 1.0-self.mask

        self.nOtf_AO = int(2*self.kc/self.dk)
        self.nOtf_AO += self.nOtf_AO % 2

        # Comb samples involved in antialising
        n_times = min(4, max(2, int(np.ceil(self.nOtf/self.nOtf_AO/2))) )
        ids = []
        for mi in range(-n_times,n_times):
            for ni in range(-n_times,n_times):
                if mi or ni: #exclude (0,0)
                    ids.append([mi,ni])
        ids = np.array(ids)

        m = torch.tensor(ids[:,0], device=self.device)
        n = torch.tensor(ids[:,1], device=self.device)
        self.N_combs = m.shape[0]

        corrected_ROI = slice(self.nOtf//2-self.nOtf_AO//2, self.nOtf//2+self.nOtf_AO//2)
        corrected_ROI = (corrected_ROI,corrected_ROI)

        self.mask_AO = self.mask[corrected_ROI]
        self.mask_corrected_AO = self.mask_corrected[corrected_ROI]
        self.mask_corrected_AO_1_1  = torch.unsqueeze(torch.unsqueeze(self.mask_corrected_AO,2),3)

        self.kx_AO = self.kx[corrected_ROI]
        self.ky_AO = self.ky[corrected_ROI]
        self.k_AO  = self.k [corrected_ROI]
        self.k2_AO = self.k2[corrected_ROI]

        # Matrix repetitions and dimensions expansion to avoid in runtime
        self.kx_1_1 = torch.unsqueeze(torch.unsqueeze(self.kx_AO,2),3)
        self.ky_1_1 = torch.unsqueeze(torch.unsqueeze(self.ky_AO,2),3)
        self.k_1_1  = torch.unsqueeze(torch.unsqueeze(self.k_AO, 2),3)

        # For NGS-like alising 2nd dimension is used to store combs information
        self.km = self.kx_1_1.repeat([1,1,self.N_combs,1]) - torch.unsqueeze(torch.unsqueeze(torch.unsqueeze(m/self.WFS_d_sub,0),0),3)
        self.kn = self.ky_1_1.repeat([1,1,self.N_combs,1]) - torch.unsqueeze(torch.unsqueeze(torch.unsqueeze(n/self.WFS_d_sub,0),0),3)

        # Initialize OTF frequencines
        self.U,self.V = torch.meshgrid(
            torch.linspace(0, self.nOtf-1, self.nOtf, device=self.device),
            torch.linspace(0, self.nOtf-1, self.nOtf, device=self.device),
            indexing = 'ij')

        self.U = (self.U-self.nOtf/2) * 2/self.nOtf
        self.V = (self.V-self.nOtf/2) * 2/self.nOtf

        self.U2  = self.U**2
        self.V2  = self.V**2
        self.UV  = self.U*self.V
        self.UV2 = self.U**2 + self.V**2

        #TODO: pathcheck
        pupil_path = self.AO_config['telescope']['PathPupil']
        pupil_apodizer = self.AO_config['telescope']['PathApodizer']

        pupil    = torch.tensor(fits.getdata(pupil_path).astype('float'), device=self.device)
        apodizer = torch.tensor(fits.getdata(pupil_apodizer).astype('float'), device=self.device)

        pupil_pix  = pupil.shape[0]
        #padded_pix = nOtf
        #padded_pix = int(pupil_pix*self.sampling)

        self.OTFs_static = torch.zeros([len(self.wvl), self.nOtf, self.nOtf], device=self.device)

        def fftAutoCorr(x):
            x_fft = fft.fft2(x)
            return fft.fftshift( fft.ifft2(x_fft*torch.conj(x_fft))/x.size(0)*x.size(1) )

        for i,samp in enumerate(self.sampling):
            padded_pix = int(pupil_pix*self.sampling[i])

            pupil_padded = torch.zeros([padded_pix, padded_pix], device=self.device)
            pupil_padded[
                padded_pix//2-pupil_pix//2 : padded_pix//2+pupil_pix//2,
                padded_pix//2-pupil_pix//2 : padded_pix//2+pupil_pix//2
            ] = pupil*apodizer

            OTF_static = torch.real( fftAutoCorr(pupil_padded) ).unsqueeze(0).unsqueeze(0)
            OTF_static = interpolate(OTF_static, size=(self.nOtf,self.nOtf), mode='bilinear', align_corners=False).squeeze(0).squeeze(0)
            self.OTFs_static[i,:,:] = OTF_static / OTF_static.max()

        self.PSD_padder = torch.nn.ZeroPad2d((self.nOtf-self.nOtf_AO)//2)

        # Piston filter
        def PistonFilter(f):
            x = (np.pi*self.D*f).cpu().numpy() #TODO: find Bessel analog for pytorch
            R = spc.j1(x)/x
            piston_filter = torch.tensor(1.0-4*R**2, device=self.device)
            piston_filter[self.nOtf_AO//2,self.nOtf_AO//2,...] *= 0.0
            return piston_filter

        self.piston_filter = PistonFilter(self.k_AO)
        self.PR = PistonFilter(torch.hypot(self.km,self.kn))


    def Update(self, data_sample, reinit_grids=True):
        self.SetDataSample(data_sample)
        if reinit_grids: self.InitGrids()


    def __init__(self, AO_config, data_samples, device=None, pixels_per_l_D=None):
        if device is None or device == 'cpu' or device == 'CPU':
            self.device = torch.device('cpu')
            self.is_gpu = False

        elif device == 'cuda' or device == 'CUDA':
            self.device = torch.device('cuda') # Will use the default CUDA device
            self.start  = torch.cuda.Event(enable_timing=True)
            self.end    = torch.cuda.Event(enable_timing=True)
            self.is_gpu = True

        super().__init__()

        self.norm_regime = 'sum'

        # Read data and initialize AO system
        self.pixels_per_l_D = pixels_per_l_D
        self.AO_config = AO_config
        self.SetDataSample(data_samples)
        self.InitGrids()


    def Controller(self, nF=1000):
        #nTh = 1
        Ts = 1.0 / self.HOloop_rate # samplingTime
        delay = self.HOloop_delay #latency
        loopGain = self.HOloop_gain

        def TransferFunctions(freq):
            z = torch.exp(-2j*np.pi*freq*Ts)
            hInt = loopGain/(1.0 - z**(-1.0))
            rtfInt = 1.0/(1 + hInt*z**(-delay))
            atfInt = hInt * z**(-delay)*rtfInt
            ntfInt = atfInt / z
            return hInt, rtfInt, atfInt, ntfInt

        f = torch.logspace(-3, torch.log10(torch.tensor([0.5/Ts])).item(), nF)
        _, _, _, ntfInt = TransferFunctions(f)
        self.noise_gain = torch.trapz(torch.abs(ntfInt)**2, f)*2*Ts

        thetaWind = torch.tensor(0.0) #torch.linspace(0, 2*np.pi-2*np.pi/nTh, nTh)
        costh = torch.cos(thetaWind)
        
        fi = -self.vx*self.kx_1_1*costh - self.vy*self.ky_1_1*costh
        _, _, atfInt, ntfInt = TransferFunctions(fi)

        # AO transfer function
        self.h1 = self.Cn2_weights * atfInt #/nTh
        self.h2 = self.Cn2_weights * abs(atfInt)**2 #/nTh
        self.hn = self.Cn2_weights * abs(ntfInt)**2 #/nTh

        self.h1 = torch.sum(self.h1,axis=(2,3))
        self.h2 = torch.sum(self.h2,axis=(2,3))
        self.hn = torch.sum(self.hn,axis=(2,3))


    def ReconstructionFilter(self, r0, L0, WFS_noise_var):
        Av = torch.sinc(self.WFS_d_sub*self.kx_AO)*torch.sinc(self.WFS_d_sub*self.ky_AO) * torch.exp(1j*np.pi*self.WFS_d_sub*(self.kx_AO+self.ky_AO))
        self.SxAv = 2j*np.pi*self.kx_AO*self.WFS_d_sub*Av
        self.SyAv = 2j*np.pi*self.ky_AO*self.WFS_d_sub*Av

        MV = 0
        Wn = WFS_noise_var/(2*self.kc)**2

        self.W_atm = self.cte*r0**(-5/3)*(self.k2_AO + 1/L0**2)**(-11/6)*(self.wvl/self.GS_wvl)**2 #TODO: check for SPHERE
        gPSD = torch.abs(self.SxAv)**2 + torch.abs(self.SyAv)**2 + MV*Wn/self.W_atm
        self.Rx = torch.conj(self.SxAv) / gPSD
        self.Ry = torch.conj(self.SyAv) / gPSD
        self.Rx[self.nOtf_AO//2, self.nOtf_AO//2] *= 0
        self.Ry[self.nOtf_AO//2, self.nOtf_AO//2] *= 0


    def SpatioTemporalPSD(self):
        A = torch.ones([self.nOtf_AO, self.nOtf_AO], device=self.device) #TODO: fix it. A should be initialized differently
        Ff = self.Rx*self.SxAv + self.Ry*self.SyAv
        psd_ST = (1+abs(Ff)**2 * self.h2 - 2*torch.real(Ff*self.h1*A)) * self.W_atm * self.mask_corrected_AO
        return psd_ST


    def NoisePSD(self, WFS_noise_var):
        noisePSD = abs(self.Rx**2 + self.Ry**2) / (2*self.kc)**2
        noisePSD = noisePSD * self.piston_filter * self.noise_gain * WFS_noise_var * self.mask_corrected_AO
        return noisePSD


    def AliasingPSD(self, r0, L0):
        T = self.WFS_det_clock_rate / self.HOloop_rate
        td = T * self.HOloop_delay

        Rx1 = torch.unsqueeze(torch.unsqueeze(2j*np.pi*self.WFS_d_sub * self.Rx,2),3)
        Ry1 = torch.unsqueeze(torch.unsqueeze(2j*np.pi*self.WFS_d_sub * self.Ry,2),3)

        W_mn = (self.km**2 + self.kn**2 + 1/L0**2)**(-11/6)
        Q = (Rx1*self.km + Ry1*self.kn) * torch.sinc(self.WFS_d_sub*self.km) * torch.sinc(self.WFS_d_sub*self.kn)

        tf = torch.unsqueeze(torch.unsqueeze(self.h1,2),3)

        avr = torch.unsqueeze(torch.unsqueeze(torch.unsqueeze(self.Cn2_weights,0),0),0) * \
            (torch.sinc(self.km*self.vx*T) * torch.sinc(self.kn*self.vy*T) * \
            torch.exp(2j*np.pi*self.km*self.vx*td) * torch.exp(2j*np.pi*self.kn*self.vy*td) * tf.repeat([1,1,self.N_combs,self.nL]))

        aliasing_PSD = torch.sum(self.PR*W_mn*abs(Q*avr.sum(axis=3,keepdim=True))**2, axis=(2,3))*self.cte*r0**(-5/3) * self.mask_corrected_AO
        return aliasing_PSD


    def VonKarmanPSD(self, r0, L0):
        return self.cte*r0**(-5/3)*(self.k2.unsqueeze(0) + 1/L0**2)**(-11/6) * self.mask.unsqueeze(0)


    def ChromatismPSD(self, r0, L0):
        wvlRef = self.wvl #TODO: polychromatic support
        W_atm = r0**(-5/3)*self.cte*(self.k2_AO + 1/L0**2)**(-11/6) * self.piston_filter #TODO: W_phi and vK spectrum
        IOR = lambda lmbd: 23.7+6839.4/(130-(lmbd*1.e6)**(-2))+45.47/(38.9-(lmbd*1.e6)**(-2))
        n2 = IOR(self.GS_wvl)
        n1 = IOR(wvlRef)
        chromatic_PSD = ((n2-n1)/n2)**2 * W_atm
        return chromatic_PSD


    def JitterCore(self, Jx, Jy, Jxy):
        u_max = self.sampling*self.D/self.wvl/(3600*180*1e3/np.pi)
        norm_fact = u_max.unsqueeze(1).unsqueeze(2)**2 * (2*np.sqrt(2*np.log(2)))**2
        Djitter = norm_fact * (Jx**2 * self.U2.unsqueeze(0) + Jy**2 * self.V2.unsqueeze(0) + 2*Jxy*self.UV.unsqueeze(0))
        return torch.exp(-0.5*Djitter) #TODO: cover Nyquist sampled case


    def NoiseVariance(self, r0): #TODO: do input of actual r0 and rescale it inside
        WFS_nPix = self.WFS_FOV / self.WFS_n_sub
        WFS_pixelScale = self.WFS_psInMas / 1e3 # [arcsec]
        # Read-out noise calculation
        nD = torch.tensor([1.0, rad2arc*self.wvl/self.WFS_d_sub/WFS_pixelScale]).max() #spot FWHM in pixels and without turbulence
        varRON = np.pi**2/3 * (self.WFS_RON**2/self.WFS_Nph**2) * (WFS_nPix**2/nD)**2
        # Photon-noise calculation
        nT = torch.tensor([1.0, torch.hypot(self.WFS_spot_FWHM.max()/1e3, rad2arc*self.WFS_wvl/r0) / WFS_pixelScale], device=self.device).max()
        varShot = np.pi**2/(2*self.WFS_Nph) * (nT/nD)**2
        # Noise variance calculation
        varNoise = self.WFS_excessive_factor * (varRON+varShot)
        return varNoise


    def DLPSF(self):
        PSF = torch.abs( fft.fftshift(fft.ifft2(fft.fftshift(self.OTF_static))) ).unsqueeze(0).unsqueeze(0)
        PSF_out = interpolate(PSF, size=(self.nPix,self.nPix), mode='area').squeeze(0).squeeze(0)
        return (PSF_out/PSF_out.sum())


    def PSD2PSF(self, r0, L0, F, dx, dy, bg, dn, Jx, Jy, Jxy):
        #WFS_noise_var2 = torch.abs(dn + self.NoiseVariance(r0_new(r0.abs(), self.GS_wvl, self.wvl)))
        #self.vx = torch.unsqueeze(torch.unsqueeze(torch.unsqueeze(self.wind_speed*torch.cos(self.wind_dir*np.pi/180.),0),0),0)
        #self.vy = torch.unsqueeze(torch.unsqueeze(torch.unsqueeze(self.wind_speed*torch.sin(self.wind_dir*np.pi/180.),0),0),0)

        #self.Controller()
        #self.ReconstructionFilter(r0.abs(), L0.abs(), WFS_noise_var2)

        # Put all contributiors together and sum up the resulting PSD
        PSD = self.VonKarmanPSD(r0.abs(),L0.abs()) #+ \
        #self.PSD_padder(
        #    self.NoisePSD(WFS_noise_var2) + \
        #    self.SpatioTemporalPSD() + \
        #    self.AliasingPSD(r0.abs(), L0) + \
        #    self.ChromatismPSD(r0.abs(), L0.abs())
        #)
        # Computing OTF from PSD
        dk = 2*self.kc/self.nOtf_AO
        #PSD *= (dk*self.wvl*1e9/2/np.pi)**2
        cov = 2*fft.fftshift(fft.fft2(fft.fftshift(PSD))) # FFT axes are set to 1,2 by PyTorch by default
        SF = (torch.abs(cov).amax(dim=(1,2)).unsqueeze(1).unsqueeze(2)-cov) * (dk*500/2/np.pi)**2
        fftPhasor = torch.exp(-np.pi*1j*self.sampling_factor.unsqueeze(1).unsqueeze(2)*(self.U*dx+self.V*dy))
        OTF_turb  = torch.exp(-0.5*SF*(2*np.pi*1e-9/self.wvl.unsqueeze(1).unsqueeze(2))**2)
        OTF = OTF_turb * self.OTFs_static * fftPhasor * self.JitterCore(Jx.abs(),Jy.abs(),Jxy.abs())

        PSF = torch.abs( fft.fftshift(fft.ifft2(fft.fftshift(OTF))) ).unsqueeze(0)
        PSF_out = interpolate(PSF, size=(self.nPix,self.nPix), mode='area').squeeze(0)

        if self.norm_regime == 'max':
            return PSF_out/torch.amax(PSF_out, dim=(1,2), keepdim=True) * F + bg
        elif self.norm_regime == 'sum':
            return PSF_out/PSF_out.sum(dim=(1,2), keepdim=True) * F + bg
        else:
            return PSF_out * F + bg


    def forward(self, x):
        inp = [x[:,i] for i in range(x.shape[1])]
        return self.PSD2PSF(*inp)


    def StartTimer(self):
        if self.is_gpu:
            self.start.record()
        else:
            self.start = time.time()


    def EndTimer(self):
        if self.is_gpu:
            self.end.record()
            torch.cuda.synchronize()
            return self.start.elapsed_time(self.end)
        else:
            self.end = time.time()
            return (self.end-self.start)*1000.0 # in [ms]

# end of class defenition

#%% -------------------------------------------------------------
toy = TipToy(config_file, data_samples, 'CUDA')
toy.norm_regime = 'max'

Nsrc = len(data_samples)

im = np.dstack([data_sample['image'] for data_sample in data_samples])

el_croppo = slice(im.shape[0]//2-32, im.shape[1]//2+32)

for i in range(im.shape[2]):
    if    toy.norm_regime == 'max': param = im[:,:,i].max()
    elif  toy.norm_regime == 'sum': param = im[:,:,i].sum()
    else: param = 1.0
    im[:,:,i] /= param

PSF_0 = torch.tensor(im, device=toy.device).permute([2,0,1])

#dx_0, dy_0 = Center(PSF_0)
#bg_0 = BackgroundEstimate(PSF_0, radius=90)

#r0  = torch.tensor(
#    [r0_new(data_sample['r0'], toy.wvl[i], 0.5e-6) for i,data_sample in enumerate(data_samples)],
#    requires_grad=True, device=toy.device).unsqueeze(1).unsqueeze(2)

r0  = torch.tensor(
    [data_sample['r0'] for data_sample in data_samples],
    requires_grad=True, device=toy.device).unsqueeze(1).unsqueeze(2)
    
L0  = torch.tensor([25.0]*3, requires_grad=False, device=toy.device).unsqueeze(1).unsqueeze(2)
F   = torch.tensor([1.0]*3,  requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)
dx  = torch.tensor([0.0]*3,  requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)
dy  = torch.tensor([0.0]*3,  requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)
bg  = torch.tensor([0.0]*3,  requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)
n   = torch.tensor([0.0]*3,  requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)
Jx  = torch.tensor([10.0]*3, requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)
Jy  = torch.tensor([10.0]*3, requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)
Jxy = torch.tensor([2.0]*3,  requires_grad=True,  device=toy.device).unsqueeze(1).unsqueeze(2)

parameters = [r0, L0, F, dx, dy, bg, n, Jx, Jy, Jxy]
#x = torch.stack(parameters).T.unsqueeze(0)

toy.StartTimer()
#PSF_1 = toy(x)
PSF_1 = toy.PSD2PSF(*parameters)

#print(toy.EndTimer())
#PSF_DL = toy.DLPSF()

for i in range(im.shape[2]):
    plt.imshow(torch.log(
        torch.hstack(
            (
                PSF_0.abs()[i, el_croppo, el_croppo],
                PSF_1.abs()[i, el_croppo, el_croppo],
                (PSF_1-PSF_0).abs()[i, el_croppo, el_croppo]
            )
        )
    ).detach().cpu())
    plt.show()
#%%

''' 
mask = 1.0 - CircularMask(im, Center(PSF_0, centered=False), 20)
t = np.linspace(-10, 10, 30)
bump = np.exp(-0.5*t**2)
bump /= np.trapz(bump) # normalize the integral to 1
kernel = bump[:, np.newaxis] * bump[np.newaxis, :]
mask = signal.fftconvolve(mask, kernel, mode='same')
mask /= mask.max()
img3 = 1.0 - torch.tensor( mask, device=toy.device )
plt.imshow(torch.log(PSF_0.abs()*img3).cpu())
'''
#%%
  
loss = nn.L1Loss(reduction='sum')

window_loss = lambda x, x_max: (x>0).float()*(0.01/x)**2 + (x<0).float()*100 + 100*(x>x_max).float()*(x-x_max)**2

def loss_fn(a,b):
    z = loss(a,b) + \
        window_loss(r0_new(r0, 0.5e-6, toy.wvl), 1.5) + \
        window_loss(Jx, 50) + \
        window_loss(Jy, 50) + \
        window_loss(Jxy, 400) + \
        window_loss(n+toy.NoiseVariance(r0_new(r0, toy.GS_wvl, toy.wvl)), 1.5)
    return z

#%
optimizer_lbfgs = OptimizeLBFGS(toy, parameters, loss_fn)

for i in range(20):
    optimizer_lbfgs.Optimize(PSF_0, [F, dx, dy, r0, n], 5)
    optimizer_lbfgs.Optimize(PSF_0, [bg], 2)
    optimizer_lbfgs.Optimize(PSF_0, [Jx, Jy, Jxy], 3)

PSF_1 = toy.PSD2PSF(*parameters)
SR = lambda PSF: (PSF.max()/PSF_DL.max() * PSF_DL.sum()/PSF.sum()).item()

#%
#optimizer_trf = OptimizeTRF(toy, parameters)
#optimizer_trf.Optimize(PSF_0)
#%

n_result = (n + toy.NoiseVariance(r0_new(r0, toy.GS_wvl, toy.wvl)) ).abs().data.item()
n_init = toy.NoiseVariance(torch.tensor(r0_new(data_test['r0'], toy.GS_wvl, 0.5e-6), device=toy.device)).item()

print("".join(['_']*52))
print('Loss:', loss_fn(PSF_1, PSF_0).item())
print("r0,r0': ({:.3f}, {:.2f})".format(data_test['r0'], r0_new(r0.data.item(), 0.5e-6, toy.wvl)))
print("F,bg:  ({:.3f}, {:.1E} )".format(F.data.item(), bg.data.item()))
print("dx,dy: ({:.2f}, {:.2f})".format(dx.data.item(), dy.data.item()))
print("Jx,Jy, Jxy: ({:.1f}, {:.1f}, {:.1f})".format(Jx.data.item(), Jy.data.item(), Jxy.data.item()))
print("n, n': ({:.2f},{:.2f})".format(n_init, n_result))

plt.imshow(torch.log( torch.hstack((PSF_0.abs()[el_croppo], PSF_1.abs()[el_croppo], ((PSF_1-PSF_0).abs()[el_croppo])) )).detach().cpu())
plt.show()

plot_radial_profile(PSF_0, PSF_1, 'TipToy', title='IRDIS PSF')
plt.show()
#la chignon et tarte


#%% =============================== MAKE DATASET ==========================================
### =======================================================================================
### =======================================================================================

# Load the SPHERE PSF database
path_fitted = 'C:/Users/akuznets/Data/SPHERE/fitted_TipToy_maxnorm/'
path_input  = 'C:/Users/akuznets/Data/SPHERE/test/'

database = SPHERE_database(path_input, path_fitted)

# Filter bad samples
bad_samples = []
for sample in database:
    buf = np.array([sample['fitted']['r0'],
                    sample['fitted']['F'],
                    sample['fitted']['n'],
                    sample['fitted']['dn'],
                    sample['fitted']['Jx'],
                    sample['fitted']['Jy'],
                    sample['fitted']['Jxy'],
                    sample['fitted']['dx'],
                    sample['fitted']['dy'],
                    sample['fitted']['bg']])

    wvl = sample['input']['spectrum']['lambda']
    r0_500 = r0_new(np.abs(sample['fitted']['r0']), 0.5e-6, wvl)
    
    n = sample['fitted']['n'] + sample['fitted']['dn']

    if np.any(np.isnan(buf)) or \
       np.isnan(sample['input']['WFS']['Nph vis']) or \
       np.abs(r0_500) > 3*sample['input']['r0'] or n > 2:
       bad_samples.append(sample['file_id']) 

for bad_sample in bad_samples:
    database.remove(bad_sample)

print(str(len(bad_samples))+' samples were filtered, '+str(len(database.data))+' samples remained')

'''
r0_500 = []
N_ph   = []
tau_0  = []
air    = []
wspd   = []
wdir   = []
seeing = []

for sample in database:
    wvl = sample['input']['spectrum']['lambda']
    r0_500.append( r0_new(np.abs(sample['fitted']['r0']), 0.5e-6, wvl) )
    seeing.append( sample['input']['seeing']['SPARTA'] )
    N_ph.append( np.log10(sample['input']['WFS']['Nph vis'] * sample['input']['WFS']['rate']*1240) )
    tau_0 .append( sample['input']['tau0']['SPARTA'] )
    air.append( sample['input']['telescope']['airmass'] )
    wspd.append( sample['input']['Wind speed']['MASSDIMM'] )
    wdir.append( sample['input']['Wind direction']['MASSDIMM'] )

r0_500 = np.array(r0_500)
N_ph = np.array(N_ph)
tau_0 = np.array(tau_0)
air = np.array(air)
wspd = np.array(wspd)
wdir = np.array(wdir)
seeing = np.array(seeing)

counts = plt.hist(N_ph, bins=20)
'''

def GetInputs(data_sample):
    #wvl     = data_sample['input']['spectrum']['lambda']
    r_0     = 3600*180/np.pi*0.976*0.5e-6 / data_sample['input']['seeing']['SPARTA'] # [m]
    tau0    = data_sample['input']['tau0']['SPARTA']
    wspeed  = data_sample['input']['Wind speed']['MASSDIMM']
    wdir    = data_sample['input']['Wind direction']['MASSDIMM']
    airmass = data_sample['input']['telescope']['airmass']
    Nph = np.log10(
        data_sample['input']['WFS']['Nph vis'] * data_sample['input']['WFS']['rate']*1240)
    input = np.array([
        r_0, tau0, wspeed, wdir, airmass, Nph,
        data_sample['fitted']['dx'],
        data_sample['fitted']['dy'],
        data_sample['fitted']['bg']])
    return input


def GetLabels(data_sample):
    toy.Update(data_sample['input'], reinit_grids=False)
    r0_500 = r0_new(np.abs(data_sample['fitted']['r0']), 0.5e-6, toy.wvl)
    WFS_noise_var = data_sample['fitted']['n'] + data_sample['fitted']['dn']
    buf =  np.array([r0_500,
                     25.0,
                     data_sample['fitted']['F'],
                     data_sample['fitted']['dx'],
                     data_sample['fitted']['dy'],
                     data_sample['fitted']['bg'],
                     WFS_noise_var,
                     data_sample['fitted']['Jx'],
                     data_sample['fitted']['Jy'],
                     data_sample['fitted']['Jxy']])
    return buf


# Filter samples with the same (most presented) wavelength
wvls = []
for sample in database:
    wvl = sample['input']['spectrum']['lambda']
    wvls.append(wvl)
wvls = np.array(wvls)

sample_ids = np.arange(len(database))
wvl_unique, _, unique_indices, counts = np.unique(wvls, return_index=True, return_inverse=True, return_counts=True)
database_wvl = database.subset(sample_ids[unique_indices==np.argmax(counts)])

# Filter bad samples manually by their file ids
bad_file_ids = [
    90,  860, 840, 839, 832, 860, 846, 844, 836, 79,  78,  77,  76,  769,
    757, 754, 752, 738, 723, 696, 681, 676, 653, 642, 63,  636, 62,  620, 
    623, 616, 615, 599, 594, 58,  57,  584, 52,  54,  521, 51,  495, 494, 468, 
    456, 433, 415, 414, 373, 368, 364, 352, 342, 338, 336, 315, 296, 297, 
    298, 291, 290, 289, 276, 264, 253, 252, 236, 234, 233, 227, 221, 220, 
    215, 214, 213, 212, 211, 209, 208, 207, 206, 204, 203, 202, 201, 200, 786,
    193, 192, 191, 190, 189, 188, 174, 172, 171, 170, 169, 166, 165, 159, 
    158, 156, 155, 143, 139, 135, 132, 130, 128, 126, 96,  92,  787, 750,
    53,  513, 490, 369, 299, 270, 263, 255, 98,  88,  87,  86,  862, 796, 781]

bad_ids = [database_wvl.find(file_id)['index'] for file_id in bad_file_ids if database_wvl.find(file_id) is not None]
good_ids = list(set(np.arange(len(database_wvl))) - set(bad_ids))
database_wvl_good = database_wvl.subset(good_ids)


def GenerateDataset(dataset, with_PSF=False):
    x = [] # inputs
    y = [] # labels

    for sample in dataset:
        input = GetInputs(sample)
        if with_PSF:
            pred = sample['input']['image']
            pred = pred / pred.max()
        else:
            pred = GetLabels(sample)
        x.append(torch.tensor(input, device=toy.device).float())
        y.append(torch.tensor(pred,  device=toy.device).float())
    
    if with_PSF:
        return torch.vstack(x), torch.dstack(y).permute([2,0,1])
    else:
        return torch.vstack(x), torch.vstack(y)


#%%
validation_ids = np.unique(np.random.randint(0, high=len(database_wvl_good), size=30, dtype=int)).tolist()
database_train, database_val = database_wvl_good.split(validation_ids)

X_train, y_train = GenerateDataset(database_train, with_PSF=False)
X_val, y_val = GenerateDataset(database_val, with_PSF=False)

print(str(X_train.shape[0])+' samples in train dataset, '+str(X_val.shape[0])+' in validation')

#%%
class Gnosis(torch.nn.Module):
    def __init__(self, input_size, hidden_size, psf_model=None, tranform_fun=lambda x:x, device='cpu'):
        self.device = device
        super(Gnosis, self).__init__()
        self.input_size  = input_size
        self.hidden_size = hidden_size

        self.fc1  = torch.nn.Linear(self.input_size, self.hidden_size*2, device=self.device)
        self.act1 = torch.nn.Tanh()
        self.fc2  = torch.nn.Linear(self.hidden_size*2, hidden_size, device=self.device)
        self.act2 = torch.nn.Tanh()
        self.fc3  = torch.nn.Linear(self.hidden_size, 10, device=self.device)

        self.inp_normalizer = torch.ones(self.input_size, device=self.device)
        self.out_normalizer = torch.ones(10, device=self.device)
        self.inp_bias = torch.zeros(self.input_size, device=self.device)
        self.out_bias = torch.zeros(10, device=self.device)

        self.psf_model = psf_model
        self.tranform_fun = tranform_fun


    def forward(self, x):
        hidden1 = self.fc1(x * self.inp_normalizer + self.inp_bias)
        act1 = self.act1(hidden1)
        hidden2 = self.fc2(act1)
        act2 = self.act2(hidden2)
        model_inp = self.fc3(act2) * self.out_normalizer + self.out_bias
        if self.psf_model is None:
            return model_inp
        else:
            return self.psf_model(self.tranform_fun(model_inp)) # to rescale r0 mostly


gnosis = Gnosis(input_size=9, hidden_size=200, device=toy.device)
gnosis.inp_normalizer = torch.tensor([5, 50, 1/50, 1/360, 1, 0.5, 2, 2, 1e6], device=toy.device).unsqueeze(0)
gnosis.inp_bias = torch.tensor([0, 0, 0, 0, -1, -3, 0, 0, 0],   device=toy.device).unsqueeze(0)
gnosis.out_normalizer = 1.0/torch.tensor([1, 1, 1, 10, 10, 2e6, 1, .1, .1, .1], device=toy.device).unsqueeze(0)

#%%
loss_fn = nn.L1Loss() #reduction='sum')
optimizer = optim.SGD([{'params': gnosis.fc1.parameters()},
                       {'params': gnosis.act1.parameters()},
                       {'params': gnosis.fc2.parameters()},
                       {'params': gnosis.act2.parameters()},
                       {'params': gnosis.fc3.parameters()}], lr=1e-3, momentum=0.9)

for i in range(6000):
    optimizer.zero_grad()
    loss = loss_fn(gnosis(X_train), y_train)
    loss.backward()
    if not i % 1000: print(loss.item())
    optimizer.step()

print('Validation accuracy: '+str(loss_fn(gnosis(X_val), y_val).item()))

#torch.save(gnosis.state_dict(), 'gnosis_weights_psfao.dict')
#gnosis.load_state_dict(torch.load('gnosis_weights_psfao.dict'))
#gnosis.eval()


#%%
def r0_transform(pred):
    pred[0][0] = r0_new(pred[0][0], wvl_unique[np.argmax(counts)], 0.5e-6)
    return pred

def PSFcomparator(data_sample):
    toy2 = TipToy(config_file, data_sample['input'], 'CUDA')
    toy2.norm_regime = 'max'
    gnosis.psf_model = toy2
    gnosis.tranform_fun = r0_transform

    x_test = torch.tensor(GetInputs(data_sample), device=toy2.device).float()
    PSF_2 = gnosis(x_test)
    A = torch.tensor(data_sample['input']['image'], device=toy2.device)
    C = torch.tensor(data_sample['fitted']['Img. fit'], device=toy2.device)
    norm = A.max()
    PSF_0 = A / norm
    PSF_1 = C / norm

    r0  = torch.tensor(r0_new(data_sample['input']['r0'], toy2.wvl, 0.5e-6), requires_grad=True, device=toy2.device)
    L0  = torch.tensor(25.0, requires_grad=True, device=toy2.device)
    F   = torch.tensor(1.0,  requires_grad=True, device=toy2.device)
    dx  = torch.tensor(0.0,  requires_grad=True, device=toy2.device)
    dy  = torch.tensor(0.0,  requires_grad=True, device=toy2.device)
    bg  = torch.tensor(0.0,  requires_grad=True, device=toy2.device)
    dn  = torch.tensor(0.0,  requires_grad=True, device=toy2.device)
    Jx  = torch.tensor(10.0, requires_grad=True, device=toy2.device)
    Jy  = torch.tensor(10.0, requires_grad=True, device=toy2.device)
    Jxy = torch.tensor(2.0,  requires_grad=True, device=toy2.device)

    PSF_3 = toy2.PSD2PSF(r0, L0, F, dx, dy, bg, dn, Jx, Jy, Jxy)
    return PSF_0, PSF_1, PSF_2, PSF_3



loss_fn = nn.L1Loss()

fit_diff = []
gnosis_diff = []
direct_diff = []

PSF_0s = []
PSF_1s = []
PSF_2s = []
PSF_3s = []

profile_0s = []
profile_1s = []
profile_2s = []
profile_3s = []

for i in range(len(database_val)):
    data_sample = database_val[i]
    PSF_0, PSF_1, PSF_2, PSF_3 = PSFcomparator(data_sample)
    fit_diff.append(loss_fn(PSF_0, PSF_1).item())
    gnosis_diff.append(loss_fn(PSF_0, PSF_2).item())
    direct_diff.append(loss_fn(PSF_0, PSF_3).item())

    PSF_0s.append(PSF_0)
    PSF_1s.append(PSF_1)
    PSF_2s.append(PSF_2)
    PSF_3s.append(PSF_3)

    profile_0s.append( radial_profile(PSF_0.detach().cpu().numpy())[:32] )
    profile_1s.append( radial_profile(PSF_1.detach().cpu().numpy())[:32] )
    profile_2s.append( radial_profile(PSF_2.detach().cpu().numpy())[:32] )
    profile_3s.append( radial_profile(PSF_3.detach().cpu().numpy())[:32] )

fit_diff = np.array(fit_diff)
gnosis_diff = np.array(gnosis_diff)
direct_diff = np.array(direct_diff)

PSF_0s = torch.dstack(PSF_0s)
PSF_1s = torch.dstack(PSF_1s)
PSF_2s = torch.dstack(PSF_2s)
PSF_3s = torch.dstack(PSF_3s)

profile_0s = np.vstack(profile_0s)
profile_1s = np.vstack(profile_1s)
profile_2s = np.vstack(profile_2s)
profile_3s = np.vstack(profile_3s)

c = profile_0s.mean(axis=0).max()

profile_0s /= c * 0.01
profile_1s /= c * 0.01
profile_2s /= c * 0.01
profile_3s /= c * 0.01

#%%

fig = plt.figure(figsize=(6,4), dpi=150)
plt.grid()

def plot_std(x,y, label, color, style):
    y_m = y.mean(axis=0)
    y_s = y.std(axis=0)
    lower_bound = y_m-y_s
    upper_bound = y_m+y_s

    plt.fill_between(x, lower_bound, upper_bound, color=color, alpha=0.3)
    plt.plot(x, y_m, label=label, color=color, linestyle=style)

x = np.arange(32)
plot_std(x, np.abs(profile_0s-profile_1s), '$\Delta$ Fit', 'royalblue', '--')
plot_std(x, np.abs(profile_0s-profile_2s), '$\Delta$ Gnosis', 'darkgreen', ':')
plot_std(x, np.abs(profile_0s-profile_3s), '$\Delta$ Direct', 'orchid', 'dashdot')

plt.title('Accuracy comparison (avg. for validation dataset)')
plt.yscale('symlog')
plt.xlim([x.min(), x.max()])
plt.legend()
plt.ylabel('Abs. relative diff., [%]')
plt.xlabel('Pixels')

#%%
fd = fit_diff.mean()
gd = gnosis_diff.mean()
dd = direct_diff.mean()

print('Fitting: '+str(np.round(dd/fd*100-100).astype('int'))+'% improvement compared to direct prediction')
print('Gnosis: ' +str(np.round(dd/gd*100-100).astype('int'))+'% improvement compared to direct prediction')

#%% ==========================================================================================
