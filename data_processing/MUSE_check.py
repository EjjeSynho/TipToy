#%%
import os
from os import path
from astropy.io import fits
import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm
from os import path
import pickle


def wavelength_to_rgb(wavelength, gamma=0.8, show_invisible=False):
    wavelength = float(wavelength)
    if wavelength >= 380 and wavelength <= 440:
        attenuation = 0.3 + 0.7 * (wavelength - 380) / (440 - 380)
        R = ((-(wavelength - 440) / (440 - 380)) * attenuation) ** gamma
        G = 0.0
        B = (1.0 * attenuation) ** gamma
    elif wavelength >= 440 and wavelength <= 490:
        R = 0.0
        G = ((wavelength - 440) / (490 - 440)) ** gamma
        B = 1.0
    elif wavelength >= 490 and wavelength <= 510:
        R = 0.0
        G = 1.0
        B = (-(wavelength - 510) / (510 - 490)) ** gamma
    elif wavelength >= 510 and wavelength <= 580:
        R = ((wavelength - 510) / (580 - 510)) ** gamma
        G = 1.0
        B = 0.0
    elif wavelength >= 580 and wavelength <= 645:
        R = 1.0
        G = (-(wavelength - 645) / (645 - 580)) ** gamma
        B = 0.0
    elif wavelength >= 645 and wavelength <= 750:
        attenuation = 0.3 + 0.7 * (750 - wavelength) / (750 - 645)
        R = (1.0 * attenuation) ** gamma
        G = 0.0
        B = 0.0
    else:
        a = 0.4
        R = a
        G = a
        B = a
        if not show_invisible:
            R = 0.0
            G = 0.0
            B = 0.0
    return (R,G,B)

def CroppedROI(im, point, win):
    ids = np.zeros(4)
    ids[0] = np.max([point[0]-win//2, 0.0])
    ids[1] = np.min([point[0]+win//2, im.shape[0]])
    ids[2] = np.max([point[1]-win//2, 0.0])
    ids[3] = np.min([point[1]+win//2, im.shape[1]])
    ids = ids.astype('uint').tolist()
    return (slice(ids[0], ids[1]), slice(ids[2], ids[3])) #TODO: check, if axis are realy meaningful y=y_im, x=x_im

def GetROIaroundMax(im, win=70):
    ROI = CroppedROI(im, np.array(im.shape)//2, win)
    return np.array(np.unravel_index(np.argmax(im[ROI]), im[ROI].shape)) + np.array([im.shape[0]//2-win//2, im.shape[1]//2-win//2])

def GetSpectrum(radius=5):
    white = hdul[3].data
    id = GetROIaroundMax(white)
    xx,yy = np.meshgrid(np.linspace(0,white.shape[1]-1,white.shape[1]), np.linspace(0,white.shape[0]-1,white.shape[0]))
    spectral_ids = np.where(np.sqrt((xx-id[1])**2 + (yy-id[0])**2) < radius)

    spectrum = np.nansum(hdul[1].data[:,spectral_ids[0],spectral_ids[1]], axis=1)
    return spectrum

def find_closest(wvl,??s):
    return np.argmin(np.abs(??s-wvl)).astype('int')


dir_path = 'C:\\Users\\akuznets\\Data\\MUSE\\DATA_raw\\'
# M.MUSE.2018-06-22T08 23 22.730.fits

files = os.listdir(dir_path)
#with fits.open(path.join(dir_path,files[0])) as hdul:

file_id = 0

#!!!! For 'M.MUSE.2021-12-06T14 34 22.646.fits' no 'ESO OBS STREHLRATIO'!!!

for file_id in range(len(files)):
    hdul = fits.open(path.join(dir_path,files[file_id]))

    start_spaxel = hdul[1].header['CRPIX3']
    num_??s = hdul[1].header['NAXIS3']-start_spaxel+1
    ???? = hdul[1].header['CD3_3' ] / 10.0
    ??_min = hdul[1].header['CRVAL3'] / 10.0
    ??s = np.arange(num_??s)*???? + ??_min
    ??_max = ??s.max()

    spectrum = GetSpectrum()

    # Remove bad wavelengths ranges
    bad_wvls = np.array([[450, 478], [577, 606]])

    bad_ids = np.zeros_like(bad_wvls.flatten(), dtype='int')
    for i,wvl in enumerate(bad_wvls.flatten()):
        bad_ids[i] = find_closest(wvl, ??s)
    bad_ids = bad_ids.reshape(bad_wvls.shape)

    valid_??s = np.ones_like(??s)
    for i in range(len(bad_ids)):
        valid_??s[bad_ids[i,0]:bad_ids[i,1]+1] = 0
        bad_wvls[i,:] = np.array([??s[bad_ids[i,0]], ??s[bad_ids[i,1]+1]])
    #??s_new = ??s[np.where(valid_??s==1)]

    # Bin data cubes
    #Before the sodium filter
    ??_bin = (bad_wvls[1][0]-bad_wvls[0][1])/3.0
    ??_bins_before = bad_wvls[0][1] + np.arange(4)*??_bin
    bin_ids_before = [find_closest(wvl, ??s) for wvl in ??_bins_before]

    #After the sodium filter
    ??_bins_num = (??_max-bad_wvls[1][1]) / np.diff(??_bins_before).mean()
    ??_bins_after = bad_wvls[1][1] + np.arange(??_bins_num+1)*??_bin
    bin_ids_after = [find_closest(wvl, ??s) for wvl in ??_bins_after]
    bins_smart = bin_ids_before + bin_ids_after
    ??_bins_smart = [?? for ?? in ??s[bins_smart]]

    #TODO: mb sodium line calibration of wavelengths grid?

    Rs = np.zeros_like(??s)
    Gs = np.zeros_like(??s)
    Bs = np.zeros_like(??s)

    for i,?? in enumerate(??s): Rs[i],Gs[i],Bs[i] = wavelength_to_rgb(??, show_invisible=True)

    Rs = Rs * valid_??s * spectrum/np.median(spectrum)
    Gs = Gs * valid_??s * spectrum/np.median(spectrum)
    Bs = Bs * valid_??s * spectrum/np.median(spectrum)
    colors = np.dstack([np.vstack([Rs, Gs, Bs])]*600).transpose(2,1,0)

    show_plots = True

    if show_plots:
        fig_handler = plt.figure(dpi=200)
        plt.imshow(colors, extent=[??s.min(), ??s.max(), 0, 120])
        plt.vlines(??_bins_smart, 0, 120, color='white') #draw bins borders
        plt.plot(??s, spectrum/spectrum.max()*120, linewidth=2.0, color='white')
        plt.plot(??s, spectrum/spectrum.max()*120, linewidth=0.5, color='blue')
        plt.xlabel(r"$\lambda$, [nm]")
        ax = plt.gca()
        ax.get_yaxis().set_visible(False)
        plt.show()

    misc_info = {
        'spectrum': spectrum,
        'wvl range': [??_min, ??_max, ????], # num = int((??_max-??_min)/????)+1
        'filtered wvls': bad_wvls,
        'wvl bins': ??_bins_smart,
    }


    white = hdul[3].data
    max_id = GetROIaroundMax(white)
    ROI = CroppedROI(white, max_id, 100)

    data_reduced = np.zeros([white[ROI].shape[0], white[ROI].shape[1], len(??_bins_smart)-1])
    std_reduced  = np.zeros([white[ROI].shape[0], white[ROI].shape[1], len(??_bins_smart)-1])
    wavelengths  = np.zeros(len(??_bins_smart)-1)

    def ChunkMean(frame, row, col, win=1, exclude=True):
        # Prevent from going outside frame's dimensions
        row1 = max(0, row-win)
        row2 = min(row+win, frame.shape[0]-1)
        col1 = max(0, col-win)
        col2 = min(col+win, frame.shape[1]-1)

        chunck = np.copy(frame[row1:row2+1, col1:col2+1])
        if np.all(np.isnan(chunck)):
            return np.nan
        if exclude:
            chunck[(row-row1, col-col1)] = np.nan # block the pixel under consideration
        return np.nanmean(chunck) # averaged value

    def HealPixels(frame):
        indexes = np.array(np.where(np.isnan(frame))).T
        while len(indexes) > 0:
            for idx in indexes:
                row,col = idx
                frame[row,col] = ChunkMean(frame, row,col)
            indexes = np.array(np.where(np.isnan(frame))).T
        return frame

    for i in range(len(bins_smart)-1):
        buf1 = np.nansum(hdul[1].data[bins_smart[i]:bins_smart[i+1],ROI[0],ROI[1]], axis=0)
        buf2 = np.nanstd(hdul[1].data[bins_smart[i]:bins_smart[i+1],ROI[0],ROI[1]], axis=0)
        data_reduced[:,:,i] = HealPixels(buf1)
        std_reduced [:,:,i] = HealPixels(buf2)
        
        wavelengths[i] = (??_bins_smart[i]+??_bins_smart[i+1])/2.0

    #if show_plots:
    #    plt.imshow(np.log(data_reduced[:,:,0]))
    #    plt.show()

    # Collect important data from the header
    data = {}
    data['date']          = hdul[0].header['DATE']
    data['date-obs']      = hdul[0].header['DATE-OBS']
    data['RA']            = hdul[0].header['RA']
    data['DEC']           = hdul[0].header['DEC']
    data['exposure']      = hdul[0].header['EXPTIME ']
    data['Strehl' ]       = hdul[0].header['HIERARCH ESO OBS STREHLRATIO']
    data['target']        = hdul[0].header['HIERARCH ESO OBS TARG NAME']
    data['airmass start'] = hdul[0].header['HIERARCH ESO TEL AIRM START']
    data['airmass end']   = hdul[0].header['HIERARCH ESO TEL AIRM END']
    data['altitude']      = hdul[0].header['HIERARCH ESO TEL ALT']
    data['azimuth']       = hdul[0].header['HIERARCH ESO TEL AZ']
    data['seeing start']  = hdul[0].header['HIERARCH ESO TEL AMBI FWHM START']
    data['seeing end']    = hdul[0].header['HIERARCH ESO TEL AMBI FWHM END']
    data['tau0']          = hdul[0].header['HIERARCH ESO TEL AMBI TAU0']
    data['temperature']   = hdul[0].header['HIERARCH ESO TEL AMBI TEMP']
    data['wind dir']      = hdul[0].header['HIERARCH ESO TEL AMBI WINDDIR']
    data['wind speed']    = hdul[0].header['HIERARCH ESO TEL AMBI WINDSP']
    hdul.close()

    packet = {
        'cube': data_reduced,
        'std': std_reduced,
        'data': data,
        'wavelengths': wavelengths,
        'misc info': misc_info
    }
    with open('C:/Users/akuznets/Data/MUSE/DATA_raw_binned/'+files[file_id].split('.fits')[0]+'.pickle', 'wb') as handle:
        pickle.dump(packet, handle, protocol=pickle.HIGHEST_PROTOCOL)


# %%
