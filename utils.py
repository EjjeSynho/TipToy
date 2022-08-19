#%%
import numpy as np
import torch
from torch import optim, nn
from astropy.io import fits
from scipy.ndimage import center_of_mass
from scipy.optimize import least_squares
from graphviz import Digraph
from skimage.transform import resize
import matplotlib.pyplot as plt

rad2mas  = 3600 * 180 * 1000 / np.pi
rad2arc  = rad2mas / 1000
deg2rad  = np.pi / 180
asec2rad = np.pi / 180 / 3600

seeing = lambda r0, lmbd: rad2arc*0.976*lmbd/r0 # [arcs]
r0_new = lambda r0, lmbd, lmbd0: r0*(lmbd/lmbd0)**1.2 # [m]
r0 = lambda seeing, lmbd: rad2arc*0.976*lmbd/seeing # [m]


def VLTpupilArea(instrument='SPHERE'): # [m2]
    if instrument == 'SPHERE':
        pupil = fits.getdata('C:/Users/akuznets/Projects/TIPTOP/P3/aoSystem/data/VLT_CALIBRATION\VLT_PUPIL/ALC2LyotStop_measured.fits').astype('float')
    else:
        raise NotImplementedError
    relative_area = pupil.sum() / (np.pi*(pupil.shape[0]//2-6.5)**2)
    true_area = np.pi * 8**2 * relative_area
    return true_area

class Photometry:
    def __init__(self):
        
        self.__InitPhotometry()
    
    def FluxFromMag(self, mag, band):
        return self.__PhotometricParameters(band)[2]/368 * 10**(-0.4*mag)
    
    def PhotonsFromMag(self, area, mag, band, sampling_time):
        return self.FluxFromMag(mag,band) * area * sampling_time

    def MagFromPhotons(self, area, Nph, band, sampling_time):
        photons = Nph / area / sampling_time
        return -2.5 * np.log10(368 * photons / self.__PhotometricParameters(band)[2])

    def __InitPhotometry(self):
        # photometry object [wavelength, bandwidth, zeroPoint]
        self.bands = {
            'U'   : [ 0.360e-6 , 0.070e-6 , 2.0e12 ],
            'B'   : [ 0.440e-6 , 0.100e-6 , 5.4e12 ],
            'V0'  : [ 0.500e-6 , 0.090e-6 , 3.3e12 ],
            'V'   : [ 0.550e-6 , 0.090e-6 , 3.3e12 ],
            'R'   : [ 0.640e-6 , 0.150e-6 , 4.0e12 ],
            'I'   : [ 0.790e-6 , 0.150e-6 , 2.7e12 ],
            'I1'  : [ 0.700e-6 , 0.033e-6 , 2.7e12 ],
            'I2'  : [ 0.750e-6 , 0.033e-6 , 2.7e12 ],
            'I3'  : [ 0.800e-6 , 0.033e-6 , 2.7e12 ],
            'I4'  : [ 0.700e-6 , 0.100e-6 , 2.7e12 ],
            'I5'  : [ 0.850e-6 , 0.100e-6 , 2.7e12 ],
            'I6'  : [ 1.000e-6 , 0.100e-6 , 2.7e12 ],
            'I7'  : [ 0.850e-6 , 0.300e-6 , 2.7e12 ],
            'R2'  : [ 0.650e-6 , 0.300e-6 , 7.92e12],
            'R3'  : [ 0.600e-6 , 0.300e-6 , 7.92e12],
            'R4'  : [ 0.670e-6 , 0.300e-6 , 7.92e12],
            'I8'  : [ 0.750e-6 , 0.100e-6 , 2.7e12 ],
            'I9'  : [ 0.850e-6 , 0.300e-6 , 7.36e12],
            'J'   : [ 1.215e-6 , 0.260e-6 , 1.9e12 ],
            'H'   : [ 1.654e-6 , 0.290e-6 , 1.1e12 ],
            'Kp'  : [ 2.1245e-6, 0.351e-6 , 6e11   ],
            'Ks'  : [ 2.157e-6 , 0.320e-6 , 5.5e11 ],
            'K'   : [ 2.179e-6 , 0.410e-6 , 7.0e11 ],
            'L'   : [ 3.547e-6 , 0.570e-6 , 2.5e11 ],
            'M'   : [ 4.769e-6 , 0.450e-6 , 8.4e10 ],
            'Na'  : [ 0.589e-6 , 0        , 3.3e12 ],
            'EOS' : [ 1.064e-6 , 0        , 3.3e12 ]
        }
        self.__wavelengths = np.array( [v[0] for _,v in self.bands.items()] )

    def __PhotometricParameters(self, inp):
        if isinstance(inp, str):
            if inp not in self.bands.keys():
                raise ValueError('Error: there is no band with the name "'+inp+'"')
                return None
            else:
                return self.bands[inp]

        elif isinstance(inp, float):    # perform interpolation of parameters for a current wavelength
            if inp < self.__wavelengths.min() or inp > self.__wavelengths.max():
                print('Error: specified value is outside the defined wavelength range!')
                return None

            difference = np.abs(self.__wavelengths - inp)
            dtype = [('number', int), ('value', float)]

            sorted = np.sort(np.array([(num, val) for num,val in enumerate(difference)], dtype=dtype), order='value')                        

            l_1 = self.__wavelengths[sorted[0][0]]
            l_2 = self.__wavelengths[sorted[1][0]]

            if l_1 > l_2:
                l_1, l_2 = l_2, l_1

            def find_params(input):
                for _,v in self.bands.items():
                    if input == v[0]:
                        return np.array(v)

            p_1 = find_params(l_1)
            p_2 = find_params(l_2)
            weight = ( (np.array([l_1, inp, l_2])-l_1)/(l_2-l_1) )[1]

            return weight*(p_2-p_1) + p_1

        else:
            print('Incorrect input: "'+inp+'"')
            return None             


def iter_graph(root, callback):
    queue = [root]
    seen = set()
    while queue:
        fn = queue.pop()
        if fn in seen:
            continue
        seen.add(fn)
        for next_fn, _ in fn.next_functions:
            if next_fn is not None:
                queue.append(next_fn)
        callback(fn)


def register_hooks(var):
    fn_dict = {}
    def hook_c_b(fn):
        def register_grad(grad_input, grad_output):
            fn_dict[fn] = grad_input
        fn.register_hook(register_grad)
    iter_graph(var.grad_fn, hook_c_b)

    def is_bad_grad(grad_output):
        if grad_output is None:
            return False
        return grad_output.isnan().any() or (grad_output.abs() >= 1e6).any()

    def make_dot():
        node_attr = dict(style='filled',
                        shape='box',
                        align='left',
                        fontsize='12',
                        ranksep='0.1',
                        height='0.2')
        dot = Digraph(node_attr=node_attr, graph_attr=dict(size="12,12"))

        def size_to_str(size):
            return '('+(', ').join(map(str, size))+')'

        def build_graph(fn):
            if hasattr(fn, 'variable'):  # if GradAccumulator
                u = fn.variable
                node_name = 'Variable\n ' + size_to_str(u.size())
                dot.node(str(id(u)), node_name, fillcolor='lightblue')
            else:
                def grad_ord(x):
                    mins = ""
                    maxs = ""
                    y = [buf for buf in x if buf is not None]
                    for buf in y:
                        min_buf = torch.abs(buf).min().cpu().numpy().item()
                        max_buf = torch.abs(buf).max().cpu().numpy().item()

                        if min_buf < 0.1 or min_buf > 99:
                            mins += "{:.1e}".format(min_buf) + ', '
                        else:
                            mins += str(np.round(min_buf,1)) + ', '
                        if max_buf < 0.1 or max_buf > 99:
                            maxs += "{:.1e}".format(max_buf) + ', '
                        else:
                            maxs += str(np.round(max_buf,1)) + ', '
                    return mins[:-2] + ' | ' + maxs[:-2]

                assert fn in fn_dict, fn
                fillcolor = 'white'
                if any(is_bad_grad(gi) for gi in fn_dict[fn]):
                    fillcolor = 'red'
                dot.node(str(id(fn)), str(type(fn).__name__)+'\n'+grad_ord(fn_dict[fn]), fillcolor=fillcolor)
            for next_fn, _ in fn.next_functions:
                if next_fn is not None:
                    next_id = id(getattr(next_fn, 'variable', next_fn))
                    dot.edge(str(next_id), str(id(fn)))
        iter_graph(var.grad_fn, build_graph)
        return dot

    return make_dot


def BackgroundEstimate(im, radius=90):
    nPix = im.shape[0]
    buf_x, buf_y = torch.meshgrid(
        torch.linspace(-nPix//2, nPix//2, nPix, device=im.device),
        torch.linspace(-nPix//2, nPix//2, nPix, device=im.device),
        indexing = 'ij'
    )
    mask_noise = buf_x**2 + buf_y**2
    mask_noise[mask_noise < radius**2] = 0.0
    mask_noise[mask_noise > 0.0] = 1.0
    return torch.median(im[mask_noise>0.]).data


def Center(im, centered=True):
    if type(im) == torch.Tensor:
        if im.device == torch.device(type='cpu'):
            im = im.detach().numpy()
        else:
            im = im.detach().cpu().numpy()
    WoG_ROI = 16
    center = np.array(np.unravel_index(np.argmax(im), im.shape))
    crop = slice(center[0]-WoG_ROI//2, center[1]+WoG_ROI//2)
    crop = (crop, crop)
    buf = im[crop]
    WoG = np.array(center_of_mass(buf)) + im.shape[0]//2-WoG_ROI//2
    return WoG - np.array(im.shape)//2 * int(centered)


def CircularMask(img, center, radius):
    xx,yy = np.meshgrid( np.arange(0,img.shape[1]), np.arange(0,img.shape[0]) )
    mask_PSF = np.ones(img.shape[0:2])
    mask_PSF[np.sqrt((yy-center[0])**2 + (xx-center[1])**2) < radius] = 0.0
    return mask_PSF


def OptimizeLBFGS(model, loss_fun, PSF_ref, params, to_optimize, steps, verbous=True):
    last_loss = 1e16
    trigger_times = 0

    optimizer = optim.LBFGS(to_optimize, lr=10, history_size=20, max_iter=4, line_search_fn="strong_wolfe")

    for i in range(steps):
        optimizer.zero_grad()
        loss = loss_fun( model(*params), PSF_ref )
        loss.backward()

        optimizer.step( lambda: loss_fun(model(*params), PSF_ref) )
        if verbous:
            print('Loss:', loss.item(), end="\r", flush=True)

        # Early stop check
        if np.round(loss.item(),4) >= np.round(last_loss,4):
            trigger_times += 1
            if trigger_times > 1:
                #print('Yea')
                return
        last_loss = loss.item()    


class OptimizeTRF():
    def __init__(self, model, parameters) -> None:
        self.model = model
        self.parameters = parameters
        self.free_params = []

    def __unpack_params(self, X):
        to_optimize = torch.tensor(X, dtype=torch.float32, device=self.model.device)
        for free_param, param in zip(self.free_params, to_optimize):
            self.parameters[free_param] = param

    def __wrapper(self,X):
        self.__unpack_params(X)
        return self.model(*self.parameters)

    def Optimize(self, PSF_ref):
        self.free_params = np.where(
            np.array([param.requires_grad for param in self.parameters]))[0].tolist()

        X0 = torch.stack(
            [param for param in self.parameters if param.requires_grad]).detach().cpu().numpy()
            
        func = lambda x: (PSF_ref-self.__wrapper(x)).detach().cpu().numpy().reshape(-1)

        iterations = 3
        for _ in range(iterations):
            result = least_squares(func, X0, method='trf',
                                ftol=1e-9, xtol=1e-9, gtol=1e-9,
                                max_nfev=1000, verbose=1, loss="linear")
            X0 = result.x
        
        self.__unpack_params(X0)

        for free_param in self.free_params: # restore intial requires_grad from PyTorch
            self.parameters[free_param].requires_grad = True


    def FitGauss2D(self, PSF):
        nPix_crop = 16
        crop = slice(PSF.shape[0]//2-PSF.shape[0]//2, PSF.shape[0]//2+nPix_crop//2)
        PSF_cropped = torch.tensor(PSF[crop,crop], requires_grad=False, device=PSF.device)
        PSF_cropped = PSF_cropped / PSF_cropped.max()

        px, py = torch.meshgrid(
            torch.linspace(-nPix_crop/2, nPix_crop/2-1, nPix_crop, device=PSF.device),
            torch.linspace(-nPix_crop/2, nPix_crop/2-1, nPix_crop, device=PSF.device),
            indexing = 'ij')

        def Gauss2D(X):
            return X[0]*torch.exp( -((px-X[1])/(2*X[3]))**2 - ((py-X[2])/(2*X[4]))**2 )

        X0 = torch.tensor([1.0, 0.0, 0.0, 1.1, 1.1], requires_grad=True, device=PSF.device)

        loss_fn = nn.MSELoss()
        optimizer = optim.LBFGS([X0], history_size=10, max_iter=4, line_search_fn="strong_wolfe")

        for _ in range(20):
            optimizer.zero_grad()
            loss = loss_fn(Gauss2D(X0), PSF_cropped)
            loss.backward()
            optimizer.step(lambda: loss_fn(Gauss2D(X0), PSF_cropped))

        FWHM = lambda x: 2*np.sqrt(2*np.log(2)) * np.abs(x)

        return FWHM(X0[3].detach().cpu().numpy()), FWHM(X0[4].detach().cpu().numpy())

# Function to draw the radial profile of a PSF
def radial_profile(data, center=None):
    if center is None:
        center = (data.shape[0]//2, data.shape[1]//2)
    y, x = np.indices((data.shape))
    r = np.sqrt( (x-center[0])**2 + (y-center[1])**2 )
    r = r.astype('int')

    tbin = np.bincount(r.ravel(), data.ravel())
    nr = np.bincount(r.ravel())
    radialprofile = tbin / nr
    return radialprofile[0:data.shape[0]//2]


def CircPupil(samples, D=8.0, centralObstruction=1.12):
    x      = np.linspace(-1/2, 1/2, samples)*D
    xx,yy  = np.meshgrid(x,x)
    circle = np.sqrt(xx**2 + yy**2)
    obs    = circle >= centralObstruction/2
    pupil  = circle < D/2 
    return pupil * obs


def PupilVLT(samples, vangle=[0,0], petal_modes=False):
    pupil_diameter = 8.0	  # pupil diameter [m]
    secondary_diameter = 1.12 # diameter of central obstruction [m] 1.12
    alpha = 101				  # spider angle [degrees]
    spider_width = 0.039	  # spider width [m] 0.039;  spider is 39 mm
    # wide excepted on small areas where 50 mm width are reached over a length 
    # of 80 mm,near the centre of the spider (before GRAVITY modification?), 
    # see VLT-DWG-AES-11310-101010

    shx = np.cos(np.deg2rad(vangle[1]))* 101.4*np.tan(np.deg2rad(vangle[0]/60))  # shift of the obscuration on the entrance pupil [m]
    shx = np.cos(np.deg2rad(vangle[1]))* 101.4*np.tan(np.deg2rad(vangle[0]/60))  # shift of the obscuration on the entrance pupil [m]
    shy = np.sin(np.deg2rad(vangle[1]))* 101.4*np.tan(np.deg2rad(vangle[0]/60))  # shift of the obscuration on the entrance pupil [m]
    delta = pupil_diameter/samples # distance between samples [m]
    ext = 2*np.max(np.fix(np.abs(np.array([shx, shy]))))+1

    # create coordinate matrices
    x1_min = -(pupil_diameter+ext - 2*delta)/2
    x1_max = (pupil_diameter + ext)/2
    num_grid = int((x1_max-x1_min)/delta)+1

    x1 = np.linspace(x1_min, x1_max, num_grid) #int(1/delta))
    x, y = np.meshgrid(x1, x1)

    #  Member data
    mask = np.ones([num_grid, num_grid], dtype='bool')
    mask[ np.where( np.sqrt( (x-shx)**2 + (y-shy)**2 ) > pupil_diameter/2 ) ] = False
    mask[ np.where( np.sqrt( x**2 + y**2 ) < secondary_diameter/2 ) ] = False

    # Spiders
    alpha_rad = alpha * np.pi / 180
    slope     = np.tan( alpha_rad/2 )

    petal_1 = np.zeros([num_grid, num_grid], dtype='bool')
    petal_2 = np.zeros([num_grid, num_grid], dtype='bool')
    petal_3 = np.zeros([num_grid, num_grid], dtype='bool')
    petal_4 = np.zeros([num_grid, num_grid], dtype='bool')

    #North
    petal_1[ np.where(   
        (( -y > 0.039/2 + slope*(-x - secondary_diameter/2 ) + spider_width/np.sin( alpha_rad/2 )/2) & (x<0)  & (y<=0)) | \
        (( -y > 0.039/2 + slope*( x - secondary_diameter/2 ) + spider_width/np.sin( alpha_rad/2 )/2) & (x>=0) & (y<=0)) )] = True
    petal_1 *= mask

    #East 
    petal_2[ np.where(   
        (( -y < 0.039/2 + slope*( x - secondary_diameter/2 ) - spider_width/np.sin( alpha_rad/2 )/2) & (x>0) & (y<=0)) | \
        ((  y < 0.039/2 + slope*( x - secondary_diameter/2 ) - spider_width/np.sin( alpha_rad/2 )/2) & (x>0) & (y>0)) )] = True
    petal_2 *= mask
        
    #South
    petal_3[ np.where(   
        ((  y > 0.039/2 + slope*(-x - secondary_diameter/2 ) + spider_width/np.sin( alpha_rad/2 )/2) & (x<=0) & (y>0)) | \
        ((  y > 0.039/2 + slope*( x - secondary_diameter/2 ) + spider_width/np.sin( alpha_rad/2 )/2) & (x>0)  & (y>0)) )] = True
    petal_3 *= mask
        
    #West
    petal_4[ np.where(   
        (( -y < 0.039/2 + slope*(-x - secondary_diameter/2 ) - spider_width/np.sin( alpha_rad/2 )/2) & (x<0) & (y<0)) |\
        ((  y < 0.039/2 + slope*(-x - secondary_diameter/2 ) - spider_width/np.sin( alpha_rad/2 )/2) & (x<0) & (y>=0)) )] = True
    petal_4 *= mask
        
    lim_x = [ ( np.fix((shy+ext/2)/delta) ).astype('int'), ( -np.fix((-shy+ext/2)/delta) ).astype('int') ]
    lim_y = [ ( np.fix((shx+ext/2)/delta) ).astype('int'), ( -np.fix((-shx+ext/2)/delta) ).astype('int') ]

    petal_1 = resize(petal_1[ lim_x[0]:-1+lim_x[1], lim_y[0]:-1+lim_y[1] ], (samples, samples), anti_aliasing=False)
    petal_2 = resize(petal_2[ lim_x[0]:-1+lim_x[1], lim_y[0]:-1+lim_y[1] ], (samples, samples), anti_aliasing=False)
    petal_3 = resize(petal_3[ lim_x[0]:-1+lim_x[1], lim_y[0]:-1+lim_y[1] ], (samples, samples), anti_aliasing=False)
    petal_4 = resize(petal_4[ lim_x[0]:-1+lim_x[1], lim_y[0]:-1+lim_y[1] ], (samples, samples), anti_aliasing=False)

    if petal_modes:
        xx1, yy1 = np.meshgrid(np.linspace( -0.5, 0.5,  samples), np.linspace(-0.25, 0.75, samples))
        xx2, yy2 = np.meshgrid(np.linspace(-0.75, 0.25, samples), np.linspace( -0.5, 0.5,  samples))
        xx3, yy3 = np.meshgrid(np.linspace( -0.5, 0.5,  samples), np.linspace(-0.75, 0.25, samples))
        xx4, yy4 = np.meshgrid(np.linspace(-0.25, 0.75, samples), np.linspace( -0.5, 0.5,  samples))

        def normalize_petal_mode(petal, coord):
            mode = petal.astype('double') * coord
            mode -= mode.min()
            mode /= (mode.max()+mode.min())
            mode -= 0.5
            mode[np.where(petal==False)] = 0.0
            mode[np.where(petal==True)] -= mode[np.where(petal==True)].mean()
            mode /= mode[np.where(petal==True)].std()
            return mode

        tip_1 = normalize_petal_mode(petal_1, yy1)
        tip_2 = normalize_petal_mode(petal_2, yy2)
        tip_3 = normalize_petal_mode(petal_3, yy3)
        tip_4 = normalize_petal_mode(petal_4, yy4)

        tilt_1 = normalize_petal_mode(petal_1, xx1)
        tilt_2 = normalize_petal_mode(petal_2, xx2)
        tilt_3 = normalize_petal_mode(petal_3, xx3)
        tilt_4 = normalize_petal_mode(petal_4, xx4)

        return np.dstack( [petal_1, petal_2, petal_3, petal_4, tip_1, tip_2, tip_3, tip_4, tilt_1, tilt_2, tilt_3, tilt_4] )

    else:
        return petal_1 + petal_2 + petal_3 + petal_4
