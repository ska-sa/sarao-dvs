#!/usr/bin/python
"""
    Formalisation of earlier SEFD_TauAOrionA.ipynb
    Typical use 1:
        python driftscan.py /data/132598363.h5 0 "Hydra A"
    which is equivalent to
        from driftscan import analyse
        analyse(sys.argv[1], int(sys.argv[2]), sys.argv[3], saveroot=".")
        
    Typical use 2:
        import driftscan
        ds, target = driftscan.load_vis("/var/kat/archive/data/RTS/telescope_products/2014/12/02/1417562258.h5", ant=0, ant_rxSN={"m063":"l.0004"}, debug=True)
        
        bore, null_l, null_r, null_w, _HPBW = driftscan.find_nulls(ds, debug_level=1)
        
        theta_src, profile_src, S_src = driftscan.models.describe_source("Taurus A")
        par_angle = np.median(ds.parangle) * np.pi/180
        _bore_ = int(np.median(bore)
        offbore_deg = driftscan.target_offset(target, ds.timestamps[_bore_], ds.az[_bore_], ds.el[_bore_], np.mean(ds.freqs)))
        hpbw0 = np.nanpercentile(_HPBW, 5)
        hpbw0_f = np.mean(ds.channel_freqs[np.abs(_HPBW/hpbw0-1)<0.01])
        C = driftscan.models.G_bore(offbore_deg/hpbw0, hpbw0_f/1e9, ds.channel_freqs/1e9)
        if (np.min(C) < 0.99):
            print("CAUTION: source transits far from bore sight (%.2fdeg), scaling flus by >=%.3f"%(offbore_deg,np.min(C)))
        Sobs_src = lambda f_GHz,yr: S_src(f_GHz,yr,par_angle) * np.reshape(driftscan.models.G_bore(offbore_deg/hpbw0, hpbw0_f/1e9, f_GHz), (-1,1))
        
        freqs, counts2Jy, SEFD_meas, SEFD_pred, Tsys_meas, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, El = \
                driftscan.get_SEFD_ND(ds,bore,[(null_l[0],null_w),(null_r[0],null_w)],
                                      Sobs_src,theta_src/60*np.pi/180 / _HPBW,profile_src,
                                      freqmask=[(360e6,380e6),(924e6,960e6),(1084e6,1088e6)]) # Blank out MUOS, GSM & SSR
        
    @author aph@sarao.ac.za
"""
import pylab as plt
import numpy as np
import warnings
import scipy.optimize as sop
import scipy.interpolate as interp
try:
    import katdal
except: 
    print("WARNING: Failed to load katdal, proceeding with limitations!")
import katpoint
from katsemat import smooth, smooth2d, Polynomial2DFit
from katselib import mask_jumps, PDFReport
import katsemodels as models
from katsemodels import _kB_, _c_


def _ylim_pct_(data, tail_pct=10, margin_pct=0, snap_to=1):
    """ @param tail_pct: the single sided tail percentage [percent]
        @param margin_pct: min & max values are this much below & above the tails [percent]
        @param snap_to: limits are roudned to integer multiples of this number (default 1).
        @return (min,max) or possibly None """
    a, b = 1-margin_pct/100., 1+margin_pct/100.
    _data = np.ma.compressed(data) if isinstance(data, np.ma.masked_array) else data[np.isfinite(data)]
    if (len(_data) == 0):
        return None
    else:
        ylim = (a*np.percentile(_data,tail_pct), b*np.percentile(_data,100-tail_pct))
        ylim = (int(ylim[0]/snap_to)*snap_to, int(ylim[1]/snap_to+0.5)*snap_to)
        return ylim

def plot_data(x_axis,vis,y_lim=None,x_lim=None,header=None,xtag=None,ytag=None,bars=None, style="-", newfig=True, **plotargs):
    """ @param y_lim: (min,max) limits for y axis, or 'pct,tail,margin', or just 'pct' to base it on 10th & 90th percentiles with 30% margin (default None)
        @param x_lim: (min,max) limits for x axis (default None)
        @param bars: If a sequence of shape 2xN, errorbars are drawn at -row1 and +row2 relative to the data.
        @param plotargs: e.g. "errorevery=100" to control placement of error bar ticks
    """
    if newfig:
        plt.figure(figsize=(12,6))
    if bars is None:
        plt.plot(x_axis,vis,style,**plotargs)
    else:
        try:
            plt.errorbar(x_axis, vis, fmt=style, capsize=1, yerr=bars, **plotargs)
        except:
            for Pol in np.arange(len(vis[1,:])):
                plt.errorbar(x_axis, vis[:,Pol], fmt=style, capsize=1, yerr=bars[:,Pol], **plotargs)
    if y_lim and ('pct' in y_lim):
        _ypct = [int(i) for i in (y_lim+",10,30").split(",")[1:3]]
        y_lim = _ylim_pct_(vis, *_ypct)
    if (y_lim is not None) and np.all(np.isfinite(y_lim)):
        plt.ylim(y_lim)
    if x_lim is not None:
        plt.xlim(x_lim)
    if header:
        plt.title(header, fontsize=14)
    if xtag:
        plt.xlabel(xtag, fontsize=14)
    if ytag:
        plt.ylabel(ytag, fontsize=14)
    plt.grid(True)
    if ("label" in plotargs.keys()):
        plt.legend(loc='best', fontsize='small', framealpha=0.8)


def downsample(x, N, axis=-1, filter=np.nanmean, trunc=False): # TODO: find out if there's a standard implementation of this.
    """ Re-samples a copy of 'x' by averaging over non-overlapping blocks of length 'N' (using 'method') along the specified axis
        
        @param N: down-sampling factor, must be an integer divisor of x.shape[axis].
        @param axi: the axis along which to down sample, or -1 to down sample the flattened array (default -1).
        @param filter: method to apply when interpolating data (default np.nanmean).
        @param trunc: True to reduce the length of the axis to a multiple of N, thereby avoiding the possible runtime error (default False)
        @return: an array with same dimensions as 'x' but down-sampled by a factor 'N'.
    """
    if (axis<0): # "Unwrap" the index so that we don't need to worry about it further below
        axis += len(x.shape)
    if not trunc:
        assert (x.shape[axis] % N == 0), "Resampling by %d doesn't preserve the number of samples (%d) along axis %d!"%(N,x.shape[axis],axis)
    y = np.ma.array(x, copy=True, fill_value=x.fill_value) if isinstance(x, np.ma.masked_array) else np.array(x, copy=True)
    if (N > 1):
        N_axes = len(y.shape)
        for i in range(axis+1,N_axes): # Move all axes after the one we're interested in, to the front
            y = np.moveaxis(y, -1, 0)
        if trunc and (y.shape[-1] % N != 0):
            y = y[...,:int(y.shape[-1]/N)*N]
        y = np.reshape(y,[s for s in y.shape[:-1]]+[y.shape[-1]//N,N]) # Last index (f) gets split into groups of N
        y = filter(y,axis=-1)
        for i in range(axis+1,N_axes): # Move all axes that were moved earlier, back
            y = np.moveaxis(y, 0, -1)
    return y


def chan_idx(channels, bounds):
    """ @param bounds: a list or tuple of min,max channels (same units as channels) or None to pass all.
        @return: a selector based on the indices where channels fall within the bounds """
    if bounds is None:
        return slice(None) # range(0,len(freqs))
    else:
        idx = np.argwhere(np.logical_and(channels>=np.min(bounds),
                                         channels<=np.max(bounds)))
        if (len(idx) > 0):
            return slice(np.min(idx),np.max(idx)+1) # range(np.min(idx),np.max(idx))
        else:
            return slice(0,0)


def mask_where(array2d, domain1d, domainmask, axis=-1):
    """ Generates a masked array from mask intervals specified for domain1d.
        
        @param array2d: 1d or 2d array
        @param domain1d: the values of the domain for the axis to apply the mask over
        @param domainmask: list of (start,stop) intervals of values in domain1d which must be masked out, inclusive
        @param axis: specify which axis of array2d the domain relates to, in case it is not obvious (default -1)
        @return: masked array representation of array2d
    """
    if domainmask:
        if (axis < 0):
            axis = 0 if (len(domain1d)==array2d.shape[0]) else 1 # This is ambiguous for square arrays
        N_i = array2d.shape[1-axis] if (len(array2d.shape)>1) else 1
        indices = np.arange(len(domain1d))
        masked_indices = []
        for minmax in domainmask:
            masked_indices.extend(indices[chan_idx(domain1d, minmax)])
        indices[masked_indices] = -1
        mask = np.stack([indices<0]*N_i, axis=1-axis)
        return np.ma.masked_array(array2d, mask, fill_value=np.nan)
    else:
        return array2d


def _fit_bl_(vis, masks=None, polyorders=[1,1]):
    """ Fit a simple 2nd order polynomial to the first two axes of the data.
        NB: masking (numpy.ma.masked_array) of the input data is ignored for the fit; only the 'vis-baseline' result
        inherits the masking of the input data.
        
        @param vis: real-valued data.
        @param masks: optional (mask0,mask1) to select across first two axes (default None)
        @param polyorders: the orders of the polynomials to fit over the two axes (default [1,1])
        @return: (baseline, vis-baseline) both with the  same shape as 'vis'.
    """
    N_t, N_f, N_p = vis.shape
    f_mesh, t_mesh = np.meshgrid(np.arange(N_f), np.arange(N_t))
    if (masks is None) or ((masks[0] is None) and (masks[-1] is None)):
        masked = lambda x: x
    elif (masks[0] is not None) and (masks[1] is not None):
        masked = lambda x: x[np.ix_(*masks)]
    elif (masks[0] is not None):
        masked = lambda x: x[masks[0],...]
    elif (masks[1] is not None):
        masked = lambda x: x[:,masks[1],...]
    x_p, y_p = masked(t_mesh), masked(f_mesh)
    
    # The fit seems to be much improved if we remove bias in both the data, as well as in all axes with gaps in it
    x0, y0, v0 = np.mean(x_p), np.mean(y_p), np.mean(vis, axis=0)
    z_p = masked(vis - v0)
    model = Polynomial2DFit(polyorders)
    bl = [model.fit([x_p-x0, y_p-y0], z_p[...,p])([t_mesh-x0, f_mesh-y0]) for p in range(N_p)]
    bl = np.stack(bl, axis=-1) # t,f,p
    bl += v0 # Restore bias
    
    v_nb = vis - bl # The data with the fitted baseline subtracted
    return bl, v_nb

def _fit_bm_(vis, t_axis, force=False, sigmu0=None, debug=True):
    """ Fits a Gaussian, plus a first order polynomial for the baseline along the first axis of the data (which fails to converge
        if the baseline slope exceeds the beam height). This fit is repeated sequentially and independently along the second axis,
        consequently execution time scales linearly with the size of the second axis.
        
        @param vis: real-valued data arranged as (time, freq, prod)
        @param t_axis: the intervals along the first axis, in terms of which sigma & mu will be defined.
        @param force: True to return starting estimate (rather than NaN's) if solution doesn't converge (default False).
        @param sigmu0: first estimate for [sigma,mu], or None to use default starting estimate (default None)
        @return: [baseline, beam] each shaped like vis; [sigmaH,sigmaV] each shaped like vis axis 1; [muH,muV] like vis axis 1
    """
    G = lambda ampl,sigma,mu: abs(ampl)*np.exp(-1/2.*(t_axis-mu)**2/sigma**2) # 1/sigma/sqrt(2pi)*... is absorbed in amplitude term
    B = lambda oH,oV,sH,sV, aH,aV,sigmaH,sigmaV,muH,muV: np.c_[oH+sH*t_axis + G(aH,sigmaH,muH),
                                                               oV+sV*t_axis + G(aV,sigmaV,muV)]
    W = lambda oH,oV,sH,sV, aH,aV,sigmaH,sigmaV,muH,muV: np.c_[0.2 + G(1,1*sigmaH,muH), # Weights to emphasize the beam peak over the baseline
                                                               0.2 + G(1,1*sigmaV,muV)]
    
    N_t, N_f, N_p = np.shape(vis)
    assert (N_p==2), "_fit_bm_() is hard-coded for data shaped like (*,*,2), not %s"%np.shape(vis)
    
    # Starting estimates: ampl, sigma, mu
    if sigmu0 is None:
        mu0 = np.median(t_axis[np.ma.any(vis>np.nanpercentile(vis,80,axis=(0,1)), axis=(1,2))])
        sigma0 = N_t/9. # Reasonable guess to start for typical scans -- no fit expected if 4*sigma > N_t
    else:
        sigma0, mu0 = sigmu0
    if np.isnan(mu0): # This happens in some pathological cases (e.g. channel 0), return NaN's
        return (np.nan+vis), (np.nan+vis), [[np.nan]*2]*N_f, [[np.nan]*2]*N_f
    
    A0 = np.nanpercentile(vis[int(mu0-10):int(mu0+10),...].data,95,axis=0) - np.nanpercentile(vis.data,5,axis=0) # (freq,prod) Use .data since can't use ma.compressed() - it discards dimensions
    
    bl, bm, sigma, mu = [], [], [], [] # Arranged as (freq,time,prod) and (freq,prod)
    vis = vis / A0 # Normalize amplitudes, so that both pols contribute similarly to optimization metric
    for f in range(N_f): # Fit per frequency channel
        if debug and (f%10 == 0):
            print("INFO: Fitting channel %d of %d"%(f,N_f))
        v_nb = vis[:,f,:]
        p0 = [0,0, 0,0, 1,1, sigma0,sigma0, mu0,mu0]
        p, s, _,_, _,_, w = sop.fmin_bfgs(lambda p: np.nansum(W(*p)*(B(*p)-v_nb)**2), p0, full_output=True, disp=False)
        ss = np.nansum((B(*p)-v_nb)**2, axis=(0,1))
        
        # Repeat the fit if necessary
        if ((w == 1) or (np.min(p[-2:]) < 0 or np.max(p[-2:]) >= N_t)): # warning OR bore sight transit out-of-bounds = likely invalid fit
            if debug:
                print("INFO: beam fitting failed to converge on first attempt, re-trying with better starting estimates.")
            # Update p0 based on the best of the two pols and retry
            bb = np.argmin(ss) # Best of the two pols
            if ((p[-2+bb] < 0) or (p[-2+bb] >= N_t)): # In some pathological cases the lowest residual has mu out of bounds
                bb = abs(1-bb) # 0->1, 1->0
            p0 = p0[:-4] + [p[bb+len(p0[:-4])+i] for i in [0,0,2,2]] # sigma & mu from the best of the fitted pols
            p, s, _,_, _,_, w = sop.fmin_bfgs(lambda p: np.nansum(W(*p)*(B(*p)-v_nb)**2), p0, full_output=True, disp=False)
            ss = np.nansum((B(*p)-v_nb)**2, axis=(0,1))
            
            if ((w == 1) or (np.min(p[-2:]) < 0 or np.max(p[-2:]) >= N_t)): # Unrecoverable after two attempts
                if force:
                    print("WARNING: beam fitting failed to converge (SS: %g~%s), using 2nd order estimate in stead" % (s, str(ss/np.max(ss))))
                    p = p0
                else:
                    if debug:
                        print("INFO: beam fitting failed to converge (SS: %g~%s), channel %d gets NaNs instead of %s" % (s, str(ss/np.max(ss)), f, str(p)))
                    bl.append(np.nan+v_nb); bm.append(np.nan+v_nb); sigma.append([np.nan]*2); mu.append([np.nan]*2)
                    continue
            elif debug:
                print("INFO: beam fitting successful on second attempt")
        
        p_bl, p_bm = p[:-6], p[-6:]
        bl.append(B(*(list(p_bl)+[0,0,1,1,0,0]))); bm.append(B(*(list(0*p_bl)+list(p_bm))))
        sigma.append(p[-4:-2]); mu.append(p[-2:])
    bl, bm = np.stack(bl,axis=1)*A0, np.stack(bm,axis=1)*A0 # Change from (freq,time,prod) to (time,freq,prod) and reverse the earlier normalization
    sigma, mu = np.asarray(sigma), np.asarray(mu)
    
    return bl, bm, sigma, mu

def fit_bm(vis, ch_res=0, freqchans=None, timemask=None, jump_zone=0, debug=0, debug_label=""):
    """ Fit a Gaussian bump plus a baseline defined by a 2nd order polynomial to the time (spatial) axis and
        a first order polynomial over frequency.
        Note: execution time scales linearly with len(channels)/ch_res, typically at 0.3sec/channel.
        
        @param vis: data arranged as (time,freq,products)
        @param ch_res: for a speed-up, > 0 to fit beams for every "ch_res" frequency bin or <=0 to fit band average only (default 0)
        @param freqchans: selector to filter the indices of frequency channels to use exclusively to identify jumps (default None).
        @param timemask: selector to filter out samples in time (default None)
        @param jump_zone: >=0 to blank out this many samples either side of a jump, <0 for no blanking (default 0).
        @param debug: 0/False for no debugging, 1/True for showing the fitted 'mu & sigma', 2 to show the raw data and 3 for 1+2 (default 0)
        @param debug_label: text to label debug information with (default "")
        @return: baseline, beam (Power, same shapes as vis), sigma (Note 1,3), mu (Note 2,3).
                 Note 1: sigma is the standard deviation of duration of main beam transit, per freq, so HPBW = sqrt(8*ln(2))*sigma [in units of time dumps]
                 Note 2: mu is times of bore sight transit per frequency [in units of time dumps]
                 Note 3: sigma & mu are masked arrays, with non-finite and outlier values masked.
    """
    # The fitting easily fails to converge if there's too large a slope, so the approach is
    # 1. fit a rough provisional baseline on a masked sub-set (fundamental limitation in how good we can mask e.g. over frequency) 
    # 2. fit a beam + delta baseline on the residual (data-baseline), each frequency channel independently.
    # 3. combine the provisional and delta baselines to form the final baseline
    
    N_t, N_f = vis.shape[:2]
    t_axis = np.arange(N_t)
    f_axis = np.arange(N_f)
    
    if freqchans is not None:
        mask = np.full(vis.shape, True); mask[:,freqchans,:] = False # False to keep data
        vis = np.ma.masked_array(vis, mask)
    if timemask is not None:
        mask = np.full(vis.shape, True); mask[timemask,...] = False # False to keep data
        vis = np.ma.masked_array(vis, mask)
    vis, tmask, fmask = mask_jumps(vis, jump_zone=jump_zone, fill_value=np.nan) # Using nan together with np.nan* below
    tmask = ~np.any(tmask, axis=1) # True to keep data; collapsed across products, since code below doesn't yet cope with mask per pol.
    fmask = ~np.any(fmask, axis=1) # Includes freqchans
    if (debug > 2):
        plot_data(t_axis, np.nanmean(vis[:,fmask,:], axis=1), xtag="Time [samples]", ytag="Power [linear]", header=debug_label+" Provisional baseline fitting")
        plot_data(t_axis[tmask], np.nanmean(vis[tmask,:,:], axis=1), newfig=False)
    
    # 1. Fit & subtract provisional baseline through first x% and last x% of the time series.
    # Ideally this should vary over frequency, but _fit_bl_ needs regular shaped, un-masked data, an dit might not be worthwhile to transform vis to (time, angle/HPBW, prod)
    _tmask = np.array(tmask, copy=True); _tmask[N_t//4:-N_t//4] = False
    bl, vis_nb = _fit_bl_(vis, (_tmask,fmask), polyorders=[1,2])
    if (debug > 2):
        plot_data(t_axis, np.mean(bl[:,fmask,:], axis=1), newfig=False)
    
    # 2. Fit beam+delta baseline on the integrated (band average) & force a non-NaN solution.
    vis0_nb = np.ma.mean(vis_nb[:,fmask,:], axis=1) # Integrated power in H & V, over time
    dbl, bm, sigma, mu = _fit_bm_(np.moveaxis([vis0_nb],0,1), t_axis, force=True, debug=False) # passing in (time,freq,prod)
    dbl, bm, sigma, mu = np.repeat(dbl,N_f,axis=1), np.repeat(bm,N_f,axis=1), np.repeat(sigma,N_f,axis=0), np.repeat(mu,N_f,axis=0) # Repeat along (existing) freq axis
    
    if (ch_res <= 0): # Asked for the band average fits are copied across frequency
        if (debug >= 2):
            plot_data(t_axis, np.nanmean((vis-bl-dbl)[:,fmask,:],axis=1), label="Baselines subtracted", xtag="Time [samples]", ytag="Power [linear]", header=debug_label)
            plot_data(t_axis, np.nanmean(bm[:,fmask,:],axis=1), label="Fitted beam models", newfig=False)
        
    else: # 2. Fit beam+delta baseline on a per-frequency bin basis, using band average as starting estimate
        sigmu0 = [np.nanmean(sigma), np.nanmean(mu)]
        # Reduce resolution before fitting (to speed up)
        ch_res = int(ch_res)
        chans = np.asarray(downsample(f_axis[fmask], ch_res, trunc=True), int)
        vis_nb = downsample(vis_nb[:,fmask,:], ch_res, axis=1, filter=np.nanmean, trunc=True)
        # Fit each remaining channel independently, with NaN's where fit doesn't converge
        dbl, bm, sigma, mu = _fit_bm_(vis_nb, t_axis, force=False, sigmu0=sigmu0, debug=debug>0)
        # Interpolate to restore original frequency resolution
        dbl = interp.interp1d(chans, dbl, 'quadratic', axis=1, bounds_error=False)(f_axis)
        bm = interp.interp1d(chans, bm, 'quadratic', axis=1, bounds_error=False)(f_axis)
        mu = interp.interp1d(chans, mu, 'quadratic', axis=0, bounds_error=False)(f_axis)
        sigma = interp.interp1d(chans, sigma, 'quadratic', axis=0, bounds_error=False)(f_axis)
        # Mask out all suspicious results
        _sigma = np.nanmedian(sigma)
        mask = ~np.isfinite(sigma) # False to keep data
        mask[~mask] |= (np.abs(sigma[~mask]) > 2*_sigma)  | (np.abs(sigma[~mask]) < 0.5*_sigma) # Split it like this to avoid unnecessary RuntimeWarnings where sigma is nan!
        sigma = np.ma.masked_array(sigma, mask)
        mu = np.ma.masked_array(mu, mask)
        if (debug >= 2):
            fig, ax = plt.subplots(2,2, figsize=(12,10))
            fig.suptitle(debug_label)
            
            resid = ((vis-bm-bl-dbl)/np.max(bm,axis=0) * 100) # Percentage, masked
            for p in [0,1]:
                ax[1][p].set_title("Model residuals [%]")
                im = ax[1][p].imshow(resid[:,:,p], origin="lower", aspect='auto', vmin=-10,vmax=10, cmap=plt.get_cmap('viridis'))
                ax[1][p].set_xlabel("Frequency [channel]")
            ax[1][0].set_ylabel("Time [samples]"); plt.colorbar(im, ax=ax[1]) 
            
            plt.subplot(2,1,1); plt.title("Baselines subtracted")
            _a, _b = (vis-bl-dbl)[:,fmask,:], bm[:,fmask,:]
            for i in range(2): # Two halves of the band, separately
                _f = slice(int(i*_a.shape[1]/2), int((i+1)*_a.shape[1]/2))
                plt.plot(t_axis, np.nanmean(_a[:,_f,:],axis=1), '-', label="Measured %d/2"%(i+1))
                plt.plot(t_axis, np.nanmean(_b[:,_f,:],axis=1), '--', label="Fitted %d/2"%(i+1))
            plt.legend(); plt.xlabel("Time [samples]"); plt.ylabel("Power [linear]"); plt.grid(True)
    
    # 3. Update the provisional baseline so that bl+mb ~ vis
    bl += dbl
    
    if (debug & 1): # 1, 3
        fig, ax = plt.subplots(2, 1, figsize=(12,6))
        fig.suptitle(debug_label)
        ax[0].plot(f_axis, mu); ax[0].grid(True); ax[0].set_ylabel("Bore sight crossing time 'mu'\n[time samples]")
        ax[1].plot(f_axis, sigma); ax[1].grid(True); ax[1].set_ylabel("HP crossing duration 'sigma'\n[time samples]"); ax[1].set_xlabel("Frequency [channel]")
    
    return bl, bm, sigma, mu


def load_vis(f, ant=0, ant_rxSN={}, swapped_pol=False, strict=False, verbose=True, debug=False, **kwargs):
    """ Loads the dataset and provides it with work-arounds for issues with the current observation procedures.
        Also supplies auxilliary features that are employed in processing steps in this module.
        
        The returned dataset has data filtered and arranged as required for processing. It specifically comprises
        only two products (third index of dataset), arranged as [0=antHH, 1=antVV]. DO NOT use the 'corr_products'
        attribute as that will not reflect the 'swapped_pol' state. Use the hacked '_pol' attribute instead. 
       
       @param f: filename string, or an already opened h5 file
       @param ant_rxSN: {antname:rxband.sn} Early system did not reflect correct receiver ID, so override
       @param swapped_pol: True to swap the order of H & V pol around (default False)
       @param strict: True to only use data while 'track'ing (e.g. tracking the drift target), vs. all data when just not 'slew'ing  (default False)
       @param debug: True to generate figures that may aid in debugging the dataset (default False)
       @param kwargs: Early system did not reflect correct centre freq, so pass 'centre_freq='[Hz] to override
       @return: (the katdal dataset with data selected & ordered as required, drift target with the antenna set).
    """
    h5 = katdal.open(f, **kwargs) if isinstance(f,str) else f
    
    for a,sn in ant_rxSN.items():
        h5.receivers[a] = sn
    if (isinstance(ant,int)):
        ant = h5.ants[ant].name
    h5.select(ants=ant, pol=('VV','HH') if swapped_pol else ('HH','VV'), reset="T")
    h5._pol = ["H", "V"]
    if verbose:
        print(h5)
        print(h5.receivers)
    
    # Current observe script sometimes mislabels the first ND scan as "slew"
    h5.select(compscans="noise diode", scans="~stop")
    ND_scans = h5.scan_indices # Converted from list -> tuple of lists below
    if (len(ND_scans) == 3):
        ND_scans = (ND_scans[:2],ND_scans[-1:]) if (np.diff(ND_scans)[0]==1) else (ND_scans[:1],ND_scans[1:])
    else:
        ND_scans = (ND_scans[:1],ND_scans[-1:])
    def __scans_ND__():
        for scans in ND_scans:
            h5.select(scans=scans)
            yield (scans[0], h5.sensor["Antennas/array/activity"][0], h5.sensor["Observation/target"][0])
    h5.__scans_ND__ = __scans_ND__
        
    # Current observe script generates compscans with blank labels - these are spurious
    # Current observe script generates two (s="track", cs="drift") scans, but the first one is spurious and sometimes is on the
    # other side of the azimuth wrap, which causes issues with tangent plane projection in find_nulls()
    spurious_scans = [] # Scan indices that should always be ignored
    h5.select(reset="T")
    for cs in h5.compscans(): # (index,label,target)
        if (cs[1].strip() == ''):
            spurious_scans.extend([s[0] for s in h5.scans()])
        elif (cs[1] == 'drift'):
            si = [s[0] for s in h5.scans() if (s[1]=='track')]
            if (len(si) > 1): # Multiple s=track,cs=drift 
                spurious_scans.append(si[0]) # The first of these is spurious
    
    # Basic select rules for extracting SEFD
    def __select_SEFD__(**selectkwargs): # If strict then just 'track', else everything except 'slew'. Filters out spurious scans.
        if strict:
            h5.select(scans="track", **selectkwargs)
            sscan_indices = set(h5.scan_indices)-set(spurious_scans)
            if (len(sscan_indices) < len(h5.scan_indices)):
                h5.select(reset='', scans=sscan_indices) # Ideally (scans=~spurious_scans)  but that doesn't work. This workaround has draw back that it resets dumps & timerange. 
        else:
            h5.select(scans="~slew", **selectkwargs)
    h5.__select_SEFD__ = __select_SEFD__
    
    h5.__select_SEFD__()
    
    target = [t for t in h5.catalogue.targets if t.body_type=='radec'][0]
    target.antenna = h5.ants[0]
    
    if debug:
        filename = h5.name.split("/")[-1].split(".")[0] # Without extension
        vis = np.abs(h5.vis[:]) # TODO: I have no idea why using vis[:] here and everywhere else speeds up the dask dataset, but it does, dramatically!
        vis_ = vis/np.percentile(vis, 1, axis=0) # Normalized as increase above baseline (robust against unlikely zeroes)
        freqs = h5.channel_freqs/1e6
        ax = plt.subplots(2,2, sharex=True, gridspec_kw={'height_ratios':[2,1]}, figsize=(16,8))[1]
        for p,P in enumerate(h5._pol):
            ax[0][p].imshow(10*np.log10(vis_[:,:,p]), aspect="auto", origin="lower", extent=(freqs[0],freqs[-1], 0,vis_.shape[0]),
                            vmin=0, vmax=6, cmap=plt.get_cmap("viridis"))
            ax[0][p].set_title("%s\n%s"%(filename,P)); ax[0][p].set_ylabel("Time [samples]")
            ax[1][p].plot(freqs, 10*np.log10(np.max(vis[:,:,p], axis=0)))
            ax[1][p].set_ylabel("max [dBcounts]"); ax[1][p].grid(True)
        ax[1][1].set_xlabel("Frequency [MHz]")
        
        chans = h5.channels
        for i in [0,1]:
            alt_x = ax[1][i].secondary_xaxis('top', functions=(lambda f:chans[0]+(chans[-1]-chans[0])/(freqs[-1]-freqs[0])*(f-freqs[0]),
                                                               lambda c:freqs[0]+(freqs[-1]-freqs[0])/(chans[-1]-chans[0])*(c-chans[0])))
            alt_x.set_xlabel("Channel #")
    return h5, target


def pred_SEFD(freqs, Tcmb, Tgal, Tatm, el_deg, RxID, D=None):
    """ Computes Tsys & SEFD from predicted values & standard models.
        @param freqs: frequencies [Hz]
        @param Tcmb: either a constant or a vector matching 'freqs' [K]
        @param Tgal: a function of frequency[Hz] [K]
        @param Tatm: a function of (frequency[Hz],elevation[deg]) [K]
        @param el_deg: elevation angle [deg]
        @param RxID: receiver ID / serial number, to load expected receiver noise profiles.
        @param D: only used if models.aperture_efficiency_models().D is undefined; diameter [m] to convert
                  aperture efficiency to effective area (default None).
        @return (eff_area [m^2], Trx [K], Tspill [K], Tsys [K], SEFD [Jy]) ordered as (freqs,pol 0=H,1=V)
    """
    Tgal = Tgal(freqs)
    Text = np.asarray([Tcmb+Tgal+Tatm(freqs,el_deg)]*2)
    Tspill = np.asarray(models.get_tip_curve_prediction(el_deg, freqs/1e6))
    Trec = np.asarray(models.get_lab_Trec(freqs/1e6,RxID))
    Tsys = Text+Tspill+Trec
    
    ant_eff = models.aperture_efficiency_models(band=models.band(freqs/1e6))
    D = ant_eff.D if (ant_eff.D > 0) else D
    Eff_area = np.asarray([ant_eff.eff["HH"](freqs/1e6), ant_eff.eff["VV"](freqs/1e6)])*(np.pi*D**2/4)
    SEFD = 2*_kB_*Tsys/Eff_area /1e-26
    
    return (Trec.transpose(), Tspill.transpose(), Text.transpose(), Tsys.transpose(), Eff_area.transpose(), SEFD.transpose()) # pol,freqs -> freqs,pol


def _get_SEFD_(vis, freqs, el_deg, MJD, bore,nulls, S_src, theta_src=0, profile_src='gaussian', enviro={}):
    """ Returns the frequency spectrum of the deflection from a calibrator source.
        @param vis: the dataset recorded power
        @param freqs: corresponding to the second axis in vis [Hz]
        @param ant, el, MJD: katpoint.Antenna, elevation in deg & modified Julian date of the observation [days].
        @param bore: time indices for source on bore sight, per frequency
        @param nulls: either specific indices or a function with arguments (vis,timestamps,frequencies) identifying the null window.
        @param S_src: 'lambda f_GHz,year' returning flux (HH, VV - corrected for parallactic angle) at the top of the atmosphere [Jy].
        @param theta_src: Extent (as per 'profile_src') of the source as a fraction of HPBW [fraction] (default 0)
        @param profile_src: either 'gaussian' or 'disc' (default 'gaussian')
        @param enviro: a dictionary of "Enviro/air_*" metadata for atmospheric effects - simply use "h5.sensor"
        @return: (freqs, counts2Jy [per pol], SEFD [total power]) the latter ordered as (freqs,pol 0=H,1=V)
    """
    vis_on = vis[bore,:,:].mean(axis=0) # Mean over time
    if callable(nulls):
        vis_off = nulls(vis,None,freqs)
    else:
        vis_off = vis[nulls,:]
    vis_off = vis_off.mean(axis=0)
    
    # Flux is absorbed as it propagates through the atmosphere: S -> S*g_atm
    opacity_at_el = models.opacity(freqs,enviro)/np.sin(el_deg*np.pi/180.0)
    g_atm = np.exp(-opacity_at_el)
    # Source-to-beam coupling factor e.g. Baars 1973: S -> S*1/K
    if ('gauss' in profile_src.lower()): # Source has a Gaussian intensity distribution, such as Taurus A,Orion A
        K = 1 + theta_src**2
    elif ('disc' in profile_src.lower()): # Source with a "top-hat" intensity distribution, such as the Moon, or Pictor A along constant declination
        K = (theta_src/2/0.6)**2 / (1 - np.exp(-(theta_src/2/0.6)**2)) # theta_src/2 is the radius
    
    yr = 1858+(365-31-14)/365.242+MJD/365.242 # Sufficiently accurate fractional year from MJD
    print("Scaling source flux for beam coupling, atmosphere at elevation %.f deg above horizon, and year %.2f"% (el_deg, yr))
    Corr = g_atm*1/K
    print("Scale factor between %.4f and %.4f"% (np.nanmin(Corr),np.nanmax(Corr)))
    S_src_at_strat = S_src(freqs/1e9, yr)
    S_src_at_ant = np.stack([Corr,Corr], -1) * S_src_at_strat # freqs,pol
    counts2Jy = S_src_at_ant/(vis_on-vis_off) # Counts 2 Jy per-pol
    SEFD_est = 2 * vis_off * counts2Jy # SEFD scaled back from per-pol to total power!
    
    return (freqs, counts2Jy, SEFD_est)


def _get_ND_(h5, counts2scale=None, y_unit="counts", freqrange=None, rfifilt=None, y_lim=None):
    """ Isolate Noise cycles to computes the ND spectra, possibly scaled. Generates a figure.
        @param counts2scale: if given (H spectrum, V spectrum) then ND spectra are scaled by this factor (default None)
        @param freqrange: like get_SEFD() & get_SEFD_ND() (default None)
        @param rfifilt: size of smoothing windows in time & freq; time window is limited to < min(ON,OFF)/3 (default None)
        @param y_lim: y limit for ND plot, as (default None)
        @return S_ND (H&V spectra) either as a fraction of the background noise, or scaled by counts2scale. Ordered as (time,pol,freq)
    """
    chans = chan_idx(h5.channel_freqs, freqrange)
    S_ND = []
    for scan in h5.__scans_ND__():
        label = "scan %d: '%s'"%(scan[0],scan[1])
        S_ND.append([])
        for pol in [0,1]:
            S = np.abs(h5.vis[1:-1,chans,pol]) # Each scan has ON & OFF; discard first & last samples because ND may not be aligned to dump boundaries
            m = np.median(S,axis=1) > np.median(S) # ON mask where average total power over the cycle is above mean toal power
            ON = np.compress(m, S, axis=0)[1:-1,:] # Observe script does not guarantee ND edges to be synchronised to dump boundaries
            OFF = np.compress(~m, S, axis=0)[1:-1,:] # It may be slow in getting on target (OFF is the first phase), but use strict=True for that case
            if (rfifilt is not None):
                if (rfifilt[0] > min(ON.shape[0], OFF.shape[0])/3.): # Limited to < shape[0]/3
                    rfifilt = (int(min(ON.shape[0], OFF.shape[0])/6.)*2-1, rfifilt[-1])
                ON = smooth2d(ON, rfifilt, axes=(0,1))
                OFF = smooth2d(OFF, rfifilt, axes=(0,1))
            ND_delta = np.mean(ON,axis=0) - np.mean(OFF,axis=0) # ON-OFF spectrum for this pol
            if (counts2scale is not None):
                ND_delta = ND_delta*counts2scale[:,pol]
            plot_data(h5.channel_freqs[chans]/1e6, ND_delta, label="%s, %s"%(h5._pol[pol],label), newfig=len(S_ND)+len(S_ND[-1])==1,
                      xtag="Frequency [MHz]", ytag="ND spectral density [%s]"%y_unit, y_lim=y_lim)
            S_ND[-1].append(ND_delta)
    
    if (len(S_ND) == 0): # No ND data
        S_ND = [[0*h5.channel_freqs, 0*h5.channel_freqs]]

    return np.asarray(S_ND) # time,freq,pol


def get_SEFD_ND(h5,bore,nulls,win_len,S_src,theta_src,profile_src,null_labels=None,freqrange=None,rfifilt=None,freqmask=None,Tcmb=2.73,Tatm=None,Tgal=None):
    """ Computes spectra of SEFD and Noise Diode equivalent flux. Also generates expected SEFD given certain estimates.
        Returned values reflect SEFD for a BACKGROUND AVERAGED BETWEEN THE NULLS ENCOUNTERED BEFORE AND AFTER TRANSIT.
        
        @param bore: time indices for source on bore sight, per frequency
        @param nulls: lists of time indices per frequency for each null (source off bore sight)
        @param win_len: the number of time indices to use around bore sight & the nulls (-1/2,+1/2)
        @param S_src: source flux function ('lambda f_GHz,year' returning flux in [HH, VV] - corrected for parallactic angle) [Jy]
        @param theta_src: Extent of the source as a fraction of HPBW [fraction]
        @param theta_src: Extent of the source (as per 'profile_src') as a fraction of HPBW [fraction] (default 0)
        @param profile_src: either 'gaussian' or 'disc'.
        @param freqrange: [fmin,fmax] (in Hz) to process & return results for, None to ommit first and last channels only (default None)
        @param rfifilt: size of smoothing windows in time & freq (default None)
        @param freqmask: list of frequency [Hz] ranges to omit from plots e.g. [(924.5e6,960e6)] (default None)
        @param Tcmb: CMB temperature (not included in Tgal) (default 2.73) [K]
        @param Tatm: Atmospheric noise temperature as func(freq_Hz,el_rad) or None to use standard (default None) [K]
        @param Tgal: Sky background temperature (excluding Tcmb) as func(freq_Hz) [K]. Default None to call models.fit_bg(nulls).
        @return: (freqs [Hz], counts2Jy, SEFD_meas [Jy], SEFD_pred [Jy], Tsys_deduced from predicted Ae [K], Trx_deduced given predicted Tspill [K],
                              Tspill_deduced given LAB Trx, Tsys_pred, Trx_expct, Tspill_expct [K], S_ND [Jy], T_ND [K], elevation [deg]) - indexed by (freqs,polarization)
    """
    null_labels = null_labels if null_labels else [str(i) for i in range(10)]
    
    # Measured SEFD at each null
    print("\nDeriving measured SEFD")
    h5.__select_SEFD__()
    el_deg = h5.el.mean()
    chans = chan_idx(h5.channel_freqs, freqrange)
    vis = np.abs(h5.vis[:,chans,:])

    if rfifilt is not None:
        vis = smooth2d(vis, rfifilt, axes=(0,1))
    counts2Jy, SEFD_meas = [], []
    for null in nulls:
        freqs, c2Jy, sefd = _get_SEFD_(vis, h5.channel_freqs[chans], el_deg, h5.mjd.mean(),
                                       bore=bore, nulls=lambda v,t,f:getvis_null(v,null,win_len),
                                       S_src=S_src, theta_src=theta_src, profile_src=profile_src, enviro=h5.sensor)
        counts2Jy.append(c2Jy)
        sefd = mask_where(sefd, freqs, freqmask)
        SEFD_meas.append(sefd)
    
    # Predicted results for assumed background (single polarization)
    ant = h5.ants[0]
    RxSN = h5.receivers[ant.name]
    fTgal = lambda n: Tgal if Tgal else models.fit_bg(h5, nulls[n][0].mean(), D=ant.diameter, debug=True)[0]
    if (Tatm is None):
        Tatm = lambda freqs, el_deg: 275*(1-np.exp(-models.opacity(freqs, h5.sensor)/np.sin(el_deg*np.pi/180))) # ITU-R P.372-11 suggests 275 as a typical number, NASA's propagation handbook 1108(02) suggests 280 K [p 7-8]
    pText, pTsys, pSEFD, Tsys_deduced = [], [], [], []
    for n in range(len(nulls)):
        print("\nPredicting SEFD at null "+null_labels[n])
        pTrx, pTspill, _pText, _pTsys, _pEffArea, _pSEFD = pred_SEFD(freqs,Tcmb,fTgal(n),Tatm,el_deg,RxSN, D=ant.diameter)
        pSEFD.append( _pSEFD )
        pTsys.append( _pTsys )
        pText.append( _pText )
        Tsys_deduced.append( SEFD_meas[n]*_pEffArea/2.0/_kB_*1e-26 )
    # pTrx, pTspill are the same for all nulls
    
    counts2Jy = np.ma.asarray(counts2Jy) # time,freq,pol
    SEFD_meas = np.ma.asarray(SEFD_meas)
    pSEFD = np.asarray(pSEFD)
    pTsys = np.asarray(pTsys)
    pText = np.asarray(pText)
    Tsys_deduced = np.ma.asarray(Tsys_deduced)
    
    # Further deduced quantities
    Trx_deduced = Tsys_deduced - pText - np.asarray([pTspill]*len(nulls))
    Tspill_deduced = Tsys_deduced - pText - np.asarray([pTrx]*len(nulls))
    
    pStyle = dict(marker="o", markevery=256, markersize=6)
    for n in range(len(nulls)):
        _ylim = _ylim_pct_(pSEFD if np.any(np.isfinite(pSEFD)) else SEFD_meas, 0, 10, snap_to=1)
        plot_data(freqs/1e6, SEFD_meas[n,:,0], newfig=(n==0), label="H, null "+null_labels[n], color="C%d"%(2*n),
                  xtag="Frequency [MHz]", ytag="SEFD [Jy]", y_lim=_ylim)
        plot_data(freqs/1e6, SEFD_meas[n,:,1], newfig=False, label="V, null "+null_labels[n], color="C%d"%(2*n+1), **pStyle)
        plot_data(freqs/1e6, pSEFD[n,:,0], newfig=False, label="Expected H, null "+null_labels[n], color="C%d"%(2*n), style="--")
        plot_data(freqs/1e6, pSEFD[n,:,1], newfig=False, label="Expected V, null "+null_labels[n], color="C%d"%(2*n+1), style="--", **pStyle)
    
    # Average over nulls
    counts2Jy, SEFD_meas = np.ma.mean(counts2Jy,axis=0), np.ma.mean(SEFD_meas,axis=0)
    Tsys_deduced, Trx_deduced, Tspill_deduced = np.ma.mean(Tsys_deduced,axis=0), np.ma.mean(Trx_deduced,axis=0), np.ma.mean(Tspill_deduced,axis=0)
    pTsys, pSEFD = np.mean(pTsys,axis=0), np.mean(pSEFD,axis=0)

    # Also get the ND spectra, if there are
    S_ND = _get_ND_(h5, counts2scale=counts2Jy, y_unit="Jy", freqrange=freqrange, rfifilt=rfifilt, y_lim='pct')
    S_ND = np.moveaxis(S_ND, 1, 2) # time,pol,freq -> time,freq,pol
    S_ND = np.ma.mean(S_ND,axis=0) # Average over independent ND measurements
    
    T_ND = S_ND * Tsys_deduced/(SEFD_meas/2.) # Remembering that SEFD _per pol_ is scaled by x2 while neither ND nor Tsys is
    
    return (freqs, counts2Jy, SEFD_meas, pSEFD, Tsys_deduced, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, T_ND, el_deg)


def getvis_null(vis, null_indices, win_len=4, debug=False):
    """ @param vis: visibilities over the frequency range of interest
        @param null_indices: vector of time index vs freq (same range as "vis")
        @param win_len: the number of indices of the window (-1/2,+1/2) for data that's returned per frequency
        @return: the vis data selected from the time window corresponding to the computed range of time indices. """
    assert vis.shape[1] == null_indices.shape[0], "vis frequency range not the same as that of null_indices!"
    
    n_a = null_indices - win_len//2
    n_z = null_indices + (win_len-win_len//2)
    _valid_freq_ = np.arange(vis.shape[1])[np.isfinite(null_indices) & (n_a >= 0) & (n_z <= vis.shape[0])]
    if debug:
        for c in _valid_freq_:
            z = vis[int(n_a[c]):int(n_z[c]),c,:].mean(axis=1)
            plot_data(range(z.shape[0]), z/z.mean(axis=0), newfig=c==0, header="Normalized values per frequency", xtag="selected time interval")
    
    vis_null = np.full((vis.shape[1], win_len, vis.shape[2]), np.nan) # Ordered as [freq,time,prod]
    for c in _valid_freq_:
        vis_null[c] = vis[int(n_a[c]):int(n_z[c]),c,:]
    return np.moveaxis(np.asarray(vis_null), 1, 0) # Re-order to [time,freq,prod]


def find_nulls(h5, cleanchans=None, HPBW=None, N_bore=-1, Nk=[1.292,2.136,2.987,3.861], theta_src=0, debug_level=0):
    """ Finds time indices when the drift scan source passes through nulls and across bore sight.
        All indices are given relative to the time axis set up by '__select_SEFD__()'.
        
        Nulls are computed from the theoretical relationship between nulls and HPBW of the parabolic illumination function.
        HPBW is derived from the dataset itself, and where no feasible values are found, the specified HPBW (whether numeric or a function)
        is used to fill in the gaps. 
        
        @param cleanchans: used to select the clean channels to use to fit bore sight transit and beam widths on
        @param HPBW: 'lambda f: x' or 1d array [rad] to override fitted widths from the dataset (default None)
        @param N_bore: Force the number of time samples to average over the bore sight crossing, else uses average of <HPBW>/16 (default -1).
        @param Nk: beam factors that give the offsets from bore sight of the nulls relative, in multiples of HPBW
                   (default [1.292,2.136,2.987,3.861] as computed from theoretical parabolic illumination pattern)
        @param theta_src: the equivalent half-power width [rad] of the target (default 0)
        @param debug_level: 0 for no debugging, 1 for some, 2 for some others and 3 for all (default 0)
        @return: (bore, nulls_before_transit, nulls_after_transit, HPBW_fitted, N_bore).
                 'bore' is the time indices while the target crosses bore sight, against frequency (1D).
                 each null_..transit is the time indices while the target is crossing that null, vs (k'th null, frequency) (2D).
                 'HPBW_fitted' gives the half power beam widths [rad] employed, 1D across frequency.
                 N_bore is the final window length to employ around bore sight & nulls.
    """
    h5.__select_SEFD__() # Reset select filters.
    T0 = h5.timestamps[0] - float(h5.start_time)
    t = np.arange(len(h5.timestamps)) # [samples]
    
    # Find the time of transit ('bore') and the beam widths ('HPBW') at each frequency
    print("INFO: Fitting transit & beam widths from the data itself.")
    # To speed up, fit only to 64 frequency channels
    beamfits = load4hpbw(h5, ch_res=len(h5.channels)//64, cleanchans=cleanchans, jump_zone=1, cached=debug_level==0, return_all=debug_level>0, debug=3)
    f, bore, sigma = beamfits[:3]
    HPBW_fitted = fit_hpbw(f, bore, sigma, theta_src=theta_src, fitchans=cleanchans, D=h5.ants[0].diameter, debug=debug_level)
    sigma2hpbw = np.sqrt(8*np.log(2)) * (2*np.pi)/(24*60*60.) # rad/sec, as used in fit_hpbw()
    
    if (HPBW is None): # HPBW not forced, so use the fitted positions, filling it in with the fitted function where it is masked
        HPBW = np.nanmean(sigma*sigma2hpbw, axis=1) # Average over products
        mask = np.any(sigma.mask, axis=1) # Mask over frequency (collapse products)
        _HPBW = HPBW_fitted(f)
        HPBW[mask] = _HPBW[mask]
    elif callable(HPBW): # Forced as a function
        HPBW = np.vectorize(HPBW)(f)
    
    # Interpolate 'bore' where it is masked, and convert to time samples relative to current selection i.e. timestamp[0]
    bore = np.mean(bore, axis=1)
    bore = interp.interp1d(f[~bore.mask], bore[~bore.mask], "cubic", axis=0, bounds_error=False, fill_value=np.median(bore))(f)
    bore = np.asarray(np.clip((bore-T0)/h5.dump_period, t[0],t[-1]), int) # [samples]
    
    N_bore = max(N_bore, int(np.nanmedian(HPBW)/(sigma2hpbw*h5.dump_period) / 16.)) # The beam changes < 1% within +-HPBW/8 interval
    
    t_bore = int(np.median(bore)) # Representative sample of bore sight transit
    print("Transit found at relative time sample %d; averaging %d time samples at each datum." % (t_bore, N_bore))
    
    # Find time indices when the source crosses the k-th null at each frequency
    target = [_t for _t in h5.catalogue.targets if _t.body_type=='radec'][0] # h5.select doesn't filter the catalogue
    D2R = np.pi/180.
    antaz, antel = h5.az[:,0]*D2R, h5.el[:,0]*D2R # deg->rad, for selected ant=0
    ll, mm = target.sphere_to_plane(antaz,antel,timestamp=h5.timestamps,projection_type="SSN",coord_system="azel") # rad
    angles = 2*np.arcsin((ll**2+mm**2)**0.5 / 2.) # Pointing offset [rad] of target relative to mechanical axis over time. For small angles this is ~ (ll**2+mm**2)**0.5
    if (debug_level in (2,3)):
        tgtaz,tgtel = target.azel(h5.timestamps)
        plot_data(np.unwrap(tgtaz)/D2R,tgtel/D2R, label="Target", xtag="Az [deg]", ytag="El [deg]")
        plot_data(antaz/D2R,antel/D2R, label="Bore sight", style='x', newfig=False)
        plt.axes().set_aspect('equal', 'datalim')

        plt.figure(figsize=(12,6)); plt.subplot(2,1,1)
        plot_data(t,np.asarray(angles)/D2R, header="Target distance from bore sight [deg]", newfig=False)
        cleanchan = h5.channels[23] if (cleanchans is None) else h5.channels[cleanchans][23] # Arbitrarily choose one
        for n in [0,1]: # target in first two nulls vs. time, for some clean channel
            flags_ch = np.abs(angles-Nk[n]*HPBW[cleanchan])<0.1*D2R
            plot_data(t[flags_ch], angles[flags_ch]/D2R, style='.', label="Null %d @ channel %d"%(n,cleanchan), newfig=False)
    # Use angles from bore sight and Nk relationship to identify nulls
    mn = lambda x: np.nan if len(x)==0 else np.mean(x) # Because np.min fails on empty, np.mean returns empty & issues a warning message
    DT = bore - t_bore
    find_null = lambda t,angles,k: np.asarray([mn(t[np.abs(angles-Nk[k]*hpbw)<hpbw/20.])+dt for dt,hpbw in zip(DT,HPBW)]) # Must subset t & angles to avoid this getting both left & right!
    null_l = [find_null(t[t<t_bore],angles[t<t_bore],k) for k in range(len(Nk))] # (k'th null, frequency)
    null_r = [find_null(t[t>t_bore],angles[t>t_bore],k) for k in range(len(Nk))]

    if (debug_level in (1,3)): # Plot the target signal along with the presumed posistions of the nulls
        bl, bm = beamfits[3:]
        vis_nb = np.abs(h5.vis[:]) - bl # "Flattened"
        vis_nb /= np.ma.max(bm, axis=0) # Normalise to fitted beam height 
        levels = [-0.1,-0.05,0,0.05,0.1] # Linear scale, fraction
        axes = plt.subplots(1, 2, sharex=True, figsize=(12,6))[1]
        axes[0].set_title("Target contribution & postulated nulls, %s (left) & %s (right). Contour spacing 0.05    [peak fraction]"%(*h5._pol,), loc='left')
        for p,ax in enumerate(axes):
            im = ax.imshow(vis_nb[...,p], origin='lower', extent=[f[0]/1e6,f[-1]/1e6,t[0],t[-1]], aspect='auto',
                           vmin=-0.15, vmax=0.15, cmap=plt.get_cmap('viridis'))
            ax.contour(vis_nb[...,p], origin='lower', extent=[f[0]/1e6,f[-1]/1e6,t[0],t[-1]], levels=levels[1:-1], colors='C0', alpha=0.5)
            for null in null_l+null_r:
                ax.plot(f/1e6, null, 'k')
            ax.set_xlabel("Frequency [MHz]")
        axes[0].set_ylabel("Time [indices]")
        plt.colorbar(im, ax=axes)
    
    return bore, null_l, null_r, HPBW, N_bore


def _debug_stats_(h5, bore_indices, nulls_indices, win_len):
    """ Plots sigma/mu spectra for bore sight & off-source & compare to expected value
    
        @param bore_indices: time indices for bore sight data per frequency (1D), relative to selection of '__select_SEFD__()'.
        @param nulls_indices: lists of time indices per frequency for each null (source off bore sight)
    """
    h5.__select_SEFD__()
    def Freq(h5,freqrange=None,ylim=None,select_dumps=None):
        """ Produces frequency spectrum plots of the currently selected dumps.
            @param freqrange: frequency start&stop to subselect (without modifying h5 selection)
            @param select_dumps: None for all time dumps, an integer range or a function(vis,timestamps,frequencies) to allow sub-selection of time interval
            @return: freq [Hz], the average spectra [linear]
        """
        vis = h5.vis[:]
        if select_dumps is not None:
            if callable(select_dumps):
                vis = select_dumps(vis, h5.timestamps, h5.channel_freqs)
            else:
                vis = vis[select_dumps,:]
        
        chans = chan_idx(h5.channel_freqs, freqrange)
        x_axis = h5.channel_freqs[chans]
        xlabel = "f [MHz]"
        
        vis = vis[:,chans,:] # Always restrict freq range after select_dumps
        vis_mean = vis.mean(axis=0)
        
        if ylim is not None:
            vis_bars = np.dstack((vis_mean-vis.min(axis=0), vis.max(axis=0)-vis_mean))
            plot_data(x_axis/1.0e6,vis_mean,y_lim=ylim,xtag=xlabel,ytag="Radiometer counts",
                      bars=vis_bars.transpose(), errorevery=30)
        return x_axis, vis_mean

    freqrange = [h5.channel_freqs[1],h5.channel_freqs[-2]]
    
    # In the limit Tsrc << Tsys, sigma = Tsys/sqrt(BW*tau), so sigma/mu = 1/sqrt(BW*tau) = const   (per polarization)
    # However, in case Tsrc >> Tsys we must use the complete sigma = sqrt(Tsys^2+2*Tsys*Tsrc+2*Tsrc^2)/sqrt(BW*tau),
    #   so expect bore sight sigma/mu = sigma/(Tsys+Tsrc) > 1/sqrt(BW*tau)
    _K = lambda Tsys,Tsrc: np.sqrt(Tsys**2+2*Tsys*Tsrc+2*Tsrc**2)/(Tsys+Tsrc)
    tau = h5.dump_period
    BW0 = abs(h5.channel_freqs[1]-h5.channel_freqs[0])
    
    # Plot statistics of bore sight data
    bore_indices = np.nanmedian(bore_indices) + np.arange(-win_len//2,win_len//2)
    Freq(h5,freqrange=freqrange,select_dumps=bore_indices,ylim='pct')
    plt.title("Spectrum with source on bore sight")
    freqs, Son = Freq(h5,freqrange=freqrange,select_dumps=bore_indices,nstd_ylim=[0,3/np.sqrt(BW0*tau)])
    plt.title("Spectrum with source on bore sight")
    
    # Plot statistics of off-source data
    for null in nulls_indices:
        freqs, Soff = Freq(h5,freqrange=freqrange,nstd_ylim=[0,3/np.sqrt(BW0*tau)],
                           select_dumps=lambda v,t,f:getvis_null(v,null,win_len))
        plt.title("Spectrum with source in null away from bore sight")
    
    Psrc = (Son-Soff)
    Psys = Soff
    K = _K(Psys,Psrc).mean(axis=0)
    print("Average bore sight noise factor K=%s" % (K))
    plot_data(np.dstack([freqs/1e6]*K.shape[0]).squeeze(), _K(Psys,Psrc)/np.sqrt(BW0*tau),
                 xtag="f [MHz]", ytag=r"$\frac{\sigma}{\mu}$", y_lim=[0,3/np.sqrt(BW0*tau)],
                 header="std/mu expected from ratios of mean on & off spectra")
    
    print("Expected boresight std/mu = %s at %.f MHz" % (K/np.sqrt(BW0*tau), np.average(freqrange)/1e6)) # May have a slope if src flux slopes
    print("Expected off-source std/mu = %s at %.f MHz" % (1/np.sqrt(BW0*tau), np.average(freqrange)/1e6)) # Must not have a slope


def target_offset(target, timestamp, az, el, freq, label="observation", debug=True):
    """ Determines the offset of the source at the specified time, relative to the pointing direction of the
        specified antenna.
        
        @param target: the katpoint.Target, with the observing antenna set if you have debug enabled.
        @param timestamp: the specified time instant [UTC seconds]
        @param ant: the katpoint.Antenna acting as observer.
        @param az, el: azimuth & elevation look angles [deg] of the antenna.
        @param freq: the frequency [Hz] to use for determining the antenna's beam width.
        @return on_delta: the offset of the source from bore sight [deg]
    """
    t_obs = katpoint.Timestamp(timestamp)
    ant = target.antenna
    on_sky = np.asarray(target.radec(t_obs, antenna=ant))*180/np.pi
    on_exp = np.asarray(target.azel(t_obs))*180/np.pi
    on_rec = np.squeeze([az, el])
    
    on_delta = np.squeeze([np.abs(on_exp[0]-on_rec[0]), np.abs(on_exp[1]-on_rec[1])])
    on_delta[0] = np.angle(np.exp(1j*on_delta[0]*np.pi/180)) * 180/np.pi # Remove 360deg ambiguity from Az angle
    if debug:
        print("UTC for %s of target: %s" % (label, t_obs))
        print("    Antenna pointing to RA %.3f hrs, DEC %.3f deg" % (on_sky[0]*24/360., on_sky[1]))
        print("    Target Az,El: %s deg"%(on_exp))
        print("    Antenna Az,El: %s deg"%(on_rec))
        HPBW = 1.22*(_c_/freq)/ant.diameter *180/np.pi # Standard illumination of circular aperture
        print("    Source within %s deg of beam bore sight (<%.3f HPBW)" % (on_delta,np.max(np.abs(on_delta)/HPBW)))
        # Report on the proximity to some special targets
        for special in ["Sun", "Moon"]:
            tgt = katpoint.Target("%s, special"%special.lower(), antenna=ant)
            print("    Distance from target to the %s %.f deg" % (special, tgt.separation(target, t_obs)*180/np.pi))
    return np.max(on_delta)


def combine(x, y, x_common=None, pctmask=100):
    """ Re-grid all results onto a common range.
    
        @param x: list of N abscissa values for each result set to combine
        @param y: list of N result sets (x[n] & y[n] dimensions must match)
        @param x_common: abscissa of final combined result set or None to construct automatically (default None)
        @param pctmask: mask points which deviate more than this percentage from a smooth curve (default 100)
        @return: x_combined, y_combined
    """
    f, _s = [], []
    for x_ in x:
        f.extend(x_); f.extend([np.nan]) # Nan's help avoid discontinuities in diff(f) below
        _s.append(1 if x_[1]>x_[0] else -1) # keep track of ordering since np.interp can't cope with descending abscissa
    if (x_common is None):
        df = np.nanmin(np.abs(np.diff(f)))
        f = np.arange(np.nanmin(f), np.nanmax(f)+df, df)
    else:
        f = x_common
    
    # Almost certainly the edge channels are bad, so ommit it on both ends
    B = 2
    y_combi = [np.interp(s*f, s*fp[B:-B], yp[B:-B], left=np.nan, right=np.nan) for s,fp,yp in zip(_s,x,y)]
    
    if (pctmask>0 and pctmask<100): # Mask out 2D data which is too far off the expected smooth curve along the second axis.
        diff = np.abs(np.diff(y_combi, axis=1))
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", "invalid value encountered in", category=RuntimeWarning)
            mask = [np.r_[np.nan, diff[p,:]] > np.nanpercentile(diff[p,:], pctmask) for p in range(len(y_combi))]
        y_combi = np.ma.masked_array(y_combi, mask, fill_value=np.nan)
        
    return f, y_combi


def summarize(results, labels=None, pol=["H","V"], header=None, pctmask=100, freqmask=None, plot_singles=True, plot_predicted=False, debug=False):
    """ Plot results against each other for comparison & combines results onto a common frequency range.
    
        @param results: a list of result sets, each one containing (freqs, counts2Jy, SEFD_meas, pSEFD, Tsys_meas, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, T_ND ...) as generated by get_SEFD_ND()
        @param pctmask: when generating the combined results, mask points which deviate more than this percentage from a smooth curve (default 100)
        @param freqmask: list of frequency [Hz] ranges to omit from plots e.g. [(924.5e6,960e6)] (default None)
        @return: f [Hz], SEFD [Jy], TSYS [K], ND [Jy], TND [K] (all but f is orderd as (freq,pol) with pol=[H,V]) 
    """
    labels = labels if labels else ["%d"%n for n in range(len(results))]
    prods = ["%s pol"%p for p in pol]
    
    def make_figure(header, tag):
        nrows = len(prods)
        fig, axes = plt.subplots(nrows,1, figsize=(12,nrows*6))
        fig.suptitle(header)
        for p,ax in zip(prods,np.atleast_1d(axes)):
            ax.set_ylabel(p+" "+tag); ax.grid(True);
        ax.set_xlabel("f [MHz]")
        return fig, axes
    
    def plot_single_and_combi(results, m_index, p_index, tag, labels, y_lim):
        # Plots measured & predicted data fom 'results', first indivdiual measurements on one figure, then another figure with average &  predicted
        if plot_singles:
            _ypct =  [int(i) for i in (y_lim+",10,30").split(",")[1:3]] if (y_lim and 'pct' in y_lim) else None
            axes = make_figure(header, tag)[1]
            for p,ax in enumerate(axes):
                for n,x in enumerate(results):
                    ax.plot(x[0]/1e6, x[m_index][:,p], label=labels[n])
                
                ax.set_ylim(_ylim_pct_(x[m_index][:,p],*_ypct) if (_ypct is not None) else y_lim)

        # Re-grid all results onto a common range & combine
        f, m_h = combine([x[0] for x in results], [x[m_index][:,0] for x in results], pctmask=pctmask) # Mask out some interference
        f, m_v = combine([x[0] for x in results], [x[m_index][:,1] for x in results], f, pctmask=pctmask)
        _h, _v = np.ma.mean(m_h,axis=0), np.ma.mean(m_v,axis=0)
        _hv = mask_where(np.dstack([_h, _v]).squeeze(),f,freqmask)

        ### Plot results with error bars to show the range
        _h, _v = _hv[:,0], _hv[:,1]
        plot_data(f/1e6, _h, label="Measured "+pol[0], xtag="Frequency [MHz]", ytag=tag, y_lim=y_lim, header=header,
                 bars=[np.ma.max(m_h,axis=0)-_h, _h-np.ma.min(m_h,axis=0)], errorevery=128, capthick=3)
        plot_data(f/1e6, _v, label="Measured "+pol[1], newfig=False,
                 bars=[np.ma.max(m_v,axis=0)-_v, _v-np.ma.min(m_v,axis=0)], errorevery=128, capthick=3)

        if (p_index == m_index):
            p_hv = _hv
        else: # Combine the predicted values in the same way as above
            f, p_h = combine([x[0] for x in results], [x[p_index][:,0] for x in results], f)
            f, p_v = combine([x[0] for x in results], [x[p_index][:,1] for x in results], f)
            p_h, p_v = np.mean(p_h,axis=0), np.mean(p_v,axis=0)
            p_hv = np.dstack([p_h, p_v]) # Not necessary to mask the predicted values
            if (plot_predicted) and np.any(np.isfinite(p_hv)):
                pStyle={"linestyle":"--", "markevery":256, "markersize":8}
                plot_data(f/1e6, p_h, label="Predicted "+pol[0], newfig=False, y_lim=y_lim, marker="^", **pStyle)
                plot_data(f/1e6, p_v, label="Predicted "+pol[1], newfig=False, marker="v", **pStyle)
        
        return f, _hv, p_hv
    
    
    # counts2Jy
    axes = make_figure(header, "Gain [Jy/#]")[1]
    for p,ax in enumerate(axes):
        for n,x in enumerate(results):
            ax.plot(x[0]/1e6, x[1][:,p], label=labels[n])
        ax.legend(); ax.set_ylim(_ylim_pct_(x[1][:,p],10,30))
 
    if debug: # Gain ratios, relative to the first result (but may fail)
        axes = make_figure(header, "#2Jy Gain ratio")[1]
        try:
            for p,ax in enumerate(axes):
                for n,x in enumerate(results[1:]):
                    ax.plot(x[0]/1e6, x[1][:,p]/results[0][1][:,p], label="%s/%s"%(labels[n+1],labels[0]))
                ax.legend(); ax.set_ylim(_ylim_pct_(x[1][:,p]/results[0][1][:,p],10,30))
        except: # May fail if frequency ranges don't match - then just ignore this step
            pass
    
    # SEFD index=2, pSEFD index=3
    f, m_SEFD, p_SEFD = plot_single_and_combi(results, 2, 3, "SEFD [Jy]", labels, 'pct')
    
    # Ae/Tsys
    plot_single_and_combi([[r[0], 2*_kB_/1e-26/r[2], 2*_kB_/1e-26/r[3]] for r in results], 1, 2,
                          "$A_e/T_{sys}$ [m$^2$/K]", labels, 'pct')
    
    # Noise Diode index=10
    f, m_ND, p_ND = plot_single_and_combi(results, 10, 10, "ND [Jy]", labels, 'pct')
    
    # TND
    f, m_TND, p_TND = plot_single_and_combi(results, 11, 11, "$T_{ND}$ [K]", labels, 'pct')
    
    # Tsys
    f, m_TSYS, p_TSYS = plot_single_and_combi(results, 4, 7, "$T_{sys}$ [K]", labels, 'pct')
    
    # Delta Tsys (measured - predicted)
    axes = make_figure(header, "$T_{sys}$(meas)-$T_{sys}$(expect) [K]")[1]
    for p,ax in enumerate(axes):
        for n,x in enumerate(results):
            ax.plot(x[0]/1e6, mask_where(x[4][:,p]-x[7][:,p],x[0],freqmask), label=labels[n])
        ax.set_ylim(-6,6)
    
    # Trec
    plot_single_and_combi(results, 5, 8, "$T_{rec}$ [K]", labels, 'pct,50,100')
    
    # Tspill + Tremainder (reflector ohmic + leakage, post-receiver contribution)
    plot_single_and_combi(results, 6, 9, "$T_{spill}+T_{rem}$ [K]", labels, 'pct,50,100')

    return f, m_SEFD, m_TSYS, m_ND, m_TND


def analyse(f, ant, source, flux_key, ant_rxSN={}, swapped_pol=False, strict=False, HPBW=None, N_bore=-1, Nk=[1.292,2.136,2.987,3.861], nulls=[(0,0)],
              fitfreqrange=None, rfifilt=[1,7], freqmask=[(360e6,380e6),(924e6,960e6),(1084e6,1092e6)],
              saveroot=None, makepdf=False, debug=False, debug_nulls=1):
    """ Generates measured and predicted SEFD results and collects it all in a PDF report, if required.
        
        @param f: filename string, or an already opened h5 file, to be passed to 'load_vis()'.
        @param ant, ant_rxSN, swapped_pol, strict: to be passed to 'load_vis()'.
        @param source: a description of the calibrator source (see 'models.describe_source()'), or None to use the defaults defined for the drift scan target.
        @param flux_key: an identifier for the source flux model, passed to 'models.describe_source()'.
        @param HPBW: something like 'lambda f: 1.2*(3e8/f)/D' [rad] to avoid fitting HPBW from the data itself (default None). Used to select data for the beam nulls.
        @param N_bore: Force the number of time samples to average over the bore sight crossing, else uses average of HPBW/20 (default -1).
        @param Nk: beam factors that give the offsets from bore sight of the nulls relative, in multiples of HPBW
                   (default [1.292,2.136,2.987,3.861] as computed from theoretical parabolic illumination pattern)
        @param nulls: pairs of indices of nulls to generate results for, zero-based or None and as (prior to, post transit) (default [(0,0)]).
        @param fitfreqrange: frequency range [Hz] that is sufficiently free from interference to be used to fit spectral baseline, or None for all (default None).
        @param rfifilt: median filter lengths for final de-noising over the time & frequency axes (default [1,7]).
        @param freqmask: list of 2-vectors for frequency bands to mask out in results, default covers MUOS(370MHz), GSM(930MHz) & SSR (1090MHz)
        @param saveroot: root folder on filesystem to save files to (default None).
        @param debug_nulls: >0 to plot null traces, >2 to plot advanced statistics (default 1).
        @return: same products as get_SEFD_ND() + [offbore_deg]
    """
    # Select all of the raw data that's relevant
    h5, target = load_vis(f, ant=ant, ant_rxSN=ant_rxSN, swapped_pol=swapped_pol, strict=strict, verbose=debug, debug=debug)
    cleanchans = chan_idx(h5.channel_freqs, fitfreqrange)
    filename = h5.name.split("/")[-1].split(" ")[0]
    ant = h5.ants[0]
    source = source if source else target.name
    
    pp = PDFReport("%s_%s_driftscan.pdf"%(filename.split(".")[0], ant.name), save=makepdf)
    try:
        pp.capture_stdout(echo=True)
        print("Drift scan %s on %s with receiver %s." % (filename, ant.name, h5.receivers[ant.name]))
        if swapped_pol:
            print("Note: The dataset has been adjusted to correct for a polarisation swap!")
        print("")
        
        src_ID, theta_src, profile_src, S_src = models.describe_source(source, flux_key=flux_key, verbose=True)
        hpw_src = (np.log(2)/2.)**.5*theta_src if (profile_src == "disc") else theta_src # from Baars 1973
        hpw_src *= np.pi/(180*60.) # arcmin to [rad]
        par_angle = np.median(h5.parangle) * np.pi/180 # Parallactic angle [rad] of antenna towards the source on bore sight
        pp.header = "Drift scan %s of %s on %s"%(filename, src_ID, ant.name)
        
        # Plot the raw data, integrated over frequency, vs relative time
        F = np.max([0]+plt.get_fignums())
        freqs = h5.channel_freqs[cleanchans]
        plt.figure(figsize=(12,6))
        plt.title("Raw drift scan time series, %g - %g MHz" % (np.min(freqs)/1e6, np.max(freqs)/1e6))
        plt.plot(np.arange(h5.vis.shape[0]), np.mean(np.abs(h5.vis[:,cleanchans,:]), axis=1)); plt.grid(True)
        plt.ylabel("Radiometer counts"); plt.xlabel("Sample Time Indices (at %g Sec Dump Rate)" % h5.dump_period)
        pp.report_fig(F+1)
    
        # Identify the bore sight and null transits
        bore, null_l, null_r, _HPBW, N_bore = find_nulls(h5, cleanchans=cleanchans, HPBW=HPBW, N_bore=N_bore, Nk=Nk, theta_src=hpw_src, debug_level=debug_nulls)
        F = np.max(plt.get_fignums())
        if (debug_nulls>0):
            pp.report_fig(F-1 - (2 if (debug_nulls in (2,3)) else 0)) # The second last figure from find_nulls(debug_level>0):fit_hpbw -> beamwidths
            pp.report_fig(F) # The last figure from find_nulls(debug_level>0): null traces
    
        if (debug_nulls>2):
            for k in [0,1]: # Check first two nulls are well defined -- ideally prefer to use k >= 1?
                getvis_null(np.abs(h5.vis[:]), null_l[k], N_bore, debug=True)
                getvis_null(np.abs(h5.vis[:]), null_r[k], N_bore, debug=True)
                _debug_stats_(h5, bore, (null_l[k], null_r[k]), N_bore) 
    
        # Correct for transit offset relative to bore sight
        _bore_ = int(np.median(bore)) # Calculate offbore_deg only for typical frequency, since offbore_deg gets slow 
        offbore_deg = target_offset(target, h5.timestamps[_bore_], h5.az[_bore_], h5.el[_bore_], np.mean(h5.freqs), "bore sight transit", debug=True)
        hpbw0, hpbw0_f = np.nanpercentile(_HPBW[cleanchans], 5), np.percentile(h5.channel_freqs[cleanchans], 95) # ~Smallest HPBW at ~highest frequency
        C = models.G_bore(offbore_deg*np.pi/180./hpbw0, hpbw0_f/1e9, h5.channel_freqs/1e9)
        print("Scaling source flux for pointing offset, by %.3f - %.3f over frequency range"%(np.max(C), np.min(C)))
        Sobs_src = lambda f_GHz, yr: S_src(f_GHz, yr, par_angle) * np.reshape(models.G_bore(offbore_deg*np.pi/180./hpbw0, hpbw0_f/1e9, f_GHz), (-1,1))
        theta_src = theta_src/60*np.pi/180/_HPBW # arcmin -> fraction of HPBW (very small impact if HPBW includes source extent)
        
        freqrange = None # Only omit first and last channels from the results to be returned
        nulls_l = [N[0] for N in nulls if N[0] is not None]
        nulls_r = [N[1] for N in nulls if N[1] is not None]
        null_groups = [null_l[N] for N in nulls_l] + [null_r[N] for N in nulls_r]
        null_labels = ["k%d"%N for N in nulls_l] + ["K%d"%N for N in nulls_r]
        print("\nNow determining measured and predicted SEFD with target in beam nulls:")
        print("    %s before transit & %s after transit" % (null_labels[:len(nulls_l)], null_labels[len(nulls_l):]))
        freqs, counts2Jy, SEFD_meas, pSEFD, Tsys_meas, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, T_ND, el_deg = \
                get_SEFD_ND(h5,bore,null_groups,N_bore,Sobs_src,theta_src,profile_src,null_labels=null_labels,freqrange=freqrange,rfifilt=rfifilt,freqmask=freqmask)
        F = np.max(plt.get_fignums())
        pp.report_fig([F-1, F]) # The last two figures from get_SEFD_ND -> SEFD & ND flux
        
        result = [freqs, counts2Jy, SEFD_meas, pSEFD, Tsys_meas, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, T_ND, el_deg, offbore_deg]
        summarize([result], pol=h5._pol, freqmask=freqmask, plot_singles=False, plot_predicted=True)
        pp.report_fig([F+i for i in [2,3,5,6]]) # Figures generated by summarize(debug=False) -> 1=counts2Jy, SEFD, Ae/Tsys, ND_Jy, T_ND, T_sys
        
        pp.report_text(r"""
        SEFD is determined from a ~1 hour drift scan of a suitable celestial calibrator.
        
        The deflection measured by the calibrator source is a spectrum formed from
                $\Delta = <BORE>-<NULL_k>$,
        where $NULL_k$ is the $k^{th}$ null adjacent to $BORE$ which represents the bore sight transit of the source.
        The angle brackets represent averaging over a time window of typically <HPBW>/16. The nominal
        bore sight transit time is determined as a single time instant for the entire dataset, while the time
        instant of each null is determined independently for each frequency channel, as follows.
        
        Time is mapped to angular offset of the source from the reported bore sight pointing coordinates.
        The angular offsets of the nulls are taken to appear at
                $\Theta_{\pm k} = \pm \{%s\}_{[k]} HPBW$
        For a circular aperture illuminated by a parabolic taper the coefficients are {1.292,2.136,2.987,3.861}.
        
        For each polarisation channel the measured SEFD is determined from the following:
                $\frac{SEFD_{perpol}}{S_{src@antenna}} = \frac{P_{NULL_k}}{P_{BORE}-P_{NULL_k}} \times 2$
        The source flux per polarisation channel $S_{src}$ is derived from total intensity $I_{src}$, polarization fraction
        $p$ and polarisation angle $\Phi$ at the top of atmosphere, as
                $S_H = \frac{1}{2}I_{src}(1-p\cos(2\Phi))$
                $S_V = \frac{1}{2}I_{src}(1+p\cos(2\Phi))$
        
        CAUTION! $SEFD_{perpol}$ above contains a scaling of x2 so that it is related to $A_e/T_{sys}$ by the conventional
                $SEFD_{perpol} = \frac{2 k_B T_{sys}}{A_e}$
        $T_{sys}$ results in this report are derived from the above, using the measured $SEFD_{perpol}$ and modeled $A_e$
        as follows
                $T_{sys,pol} = \frac{SEFD_{perpol} \times 10^{-26}}{2 k_B} A_e$
        
        Source flux $S_{src}$ is propagated from a reference plane above the atmosphere to the antenna plane by  
                $S_{src@antenna} = S_{src} e^\frac{-\tau}{\sin(el)} U(\varepsilon_\theta) \frac{\Omega_\Sigma}{\Omega_{src}}$
        The additional terms above are
            * atmospheric extinction with $\tau$ the estimated atmospheric opacity,
            * gain correction $U(\varepsilon_\theta)$ for estimated pointing offset at bore sight transit and
            * source-to-beam coupling correction factor $\frac{\Omega_\Sigma}{\Omega_{src}}$. $\Omega_\Sigma$ is the beam-weighted source solid angle.
        
        In UHF and L band the atmospheric extinction is typically less than 0.5%%. All of the sources employed
        have half-power widths less than 4 arcmin so that the beam coupling factor is less than 1%% at 1.7 GHz.
        These correction factors as well as the correction applied to compensate for pointing error, are reported
        in the processing logs.
        
        The predicted SEFD is calculated as
                $\widehat{T}_{sys} = T_{CMB} + T_{GSM} + T_{atm} + T_{spill} + T_{rx}$
                $\widehat{SEFD}_{perpol} = \frac{2 k_B \widehat{T}_{sys}}{A_e}$
        where $T_{GSM}$ is obtained from Global Sky Model maps and $T_{atm}$ is determined as per ITU-R P.676-9
        & Ippolito 1989 (eq 6.8-6). $T_{spill}$ and $A_e$ have been computed by full-wave EM techniques applied to
        the as-built model for the reflector system. $T_{rx}$ is the input-referred noise that's measured at the
        factory for each specific receiver.
        
        
        The "noise diode equivalent flux" is measured while briefly tracking the calibrator and determined as
                $S_{ND,pol} = (P_{ND_ON}-P_{ND_OFF}) \frac{S_{src@antenna}}{P_{BORE}-P_{NULL_k}}$
        The above may be scaled to an equivalent noise temperature at the waveguide port, using
        the following relation
                $T_{ND,pol} = \frac{S_{ND,pol} \times 10^{-26}}{k_B} A_e$
        """%(str(Nk)[1:-1],))
    finally:
        pp.close()
    
    if saveroot:
        save_data(saveroot, filename, ant.name, src_ID, freqs, counts2Jy, SEFD_meas, pSEFD, Tsys_meas, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, T_ND, el_deg, offbore_deg)
    return result

run_SEFD = analyse # Alias


def save_data(root,dataset_fname,antname,target, freqs, counts2Jy, SEFD_meas, pSEFD, Tsys_meas, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, T_ND, el_deg, *ig, **nored):
    """ Saves data products to CSV files named as 'root/dataset-ant-product.csv
        @param root: root folder on filesystem to save files to.
        @param dataset_fname: the filename of the raw dataset.
        @param antname, target: identifies the antenna and target that the dat awas collected from.
        @param freqs, counts2Jy, SEFD_meas, pSEFD, Tsys_meas, Trx_deduced, Tspill_deduced, pTsys, pTrx, pTspill, S_ND, T_ND, el_deg: data products as returned by 'get_SEFD_ND()'.
    """
    fnroot = "%s/%s-%s-"%(root,dataset_fname.split("/")[-1].split(".")[0],antname)
    origin = "Recorded with <%s> at %.fdegEl\nDataset %s" % (target, el_deg, dataset_fname)
    for fn,data,descr,unit in [("counts2Jy",np.c_[freqs, counts2Jy[:,0], counts2Jy[:,1]],"(Source Flux)/(P_src_on_boresight-P_src_in_null)","Jy/#"),
                               ("SEFD",np.c_[freqs, SEFD_meas[:,0], SEFD_meas[:,1]],"SEFD at calibrator background","Jy"),
                               ("S_ND",np.c_[freqs, S_ND[:,0], S_ND[:,1]],"Noise Diode equivalent Flux","Jy")]:
        np.savetxt(fnroot+fn+".csv", data, delimiter=",",
                   header="%s. %s\nfrequency [Hz]\t,H [%s]\t, V [%s]"%(descr,origin,unit,unit))

def load_data(fids, product="SEFD", root=""):
    """ Loads the results stored when analyse() completes. Re-grids all data onto a common frequency grid.
        @param fids: list of file ID's (e.g. "{epoch seconds}_sdp_l0-s0000")
        @param product: "SEFD"|"S_ND"|"counts2Jy" (default "SEFD")
        @return: freq_Hz, H_data, V_data
    """
    f_,d_H,d_V = [],[],[]
    for i in fids:
        f,x_h,x_v = np.loadtxt("%s/%s-%s.csv"%(root,i,product), delimiter=",", unpack=True)
        d_H.append(x_h)
        d_V.append(x_v)
        f_.append(f)
    # Re-grid all results onto a common range
    f, d_H = combine(f_, d_H)
    f, d_V = combine(f_, d_V)
    d_H = np.transpose(d_H)
    d_V = np.transpose(d_V)
    return f, d_H, d_V


def save_Tnd(freqs, T_ND, rx_band,rx_SN, output_dir, rfi_mask=[], ant="TBD", debug=False):
    """ Save noise diode equivalent temperatures to a model file, in the standard MeerKAT format.
        If there are lists of frequencies & T_ND then the data series will automatically be combined.
        
        @param freqs: (lists of) frequencies [Hz]
        @param T_ND: (lists of) Noise diode equivalent temperatures [K], arranged as (freqs,pol)
        @param rx_band, rx_SN: band (e.g. "u","l") & serial number (e.g. "004")
        @param output_dir: folder where csv files will be stored
        @param rfi_mask: list of frequency [Hz] ranges to omit from plots e.g. [(924.5e6,960e6)] (default [])
        @param ant: antenna on which the data was measured (default "TBD")
    """
    if (np.ndim(freqs[0]) == 0):
        TndH, TndV = T_ND[:,0], T_ND[:,1]
    else: # Lists to be combined
        f, _h = combine(freqs, [x[:,0] for x in T_ND])
        freqs, _v = combine(freqs, [x[:,1] for x in T_ND], f)
        TndH, TndV = np.ma.mean(_h,axis=0), np.ma.mean(_v,axis=0)
    
    # Mask & write to file
        
    for pol,Tdiode in enumerate([TndH,TndV]):
        Tdiode = mask_where(Tdiode, freqs, rfi_mask+[(0,1)]) # Mask out RFI-contaminated bits & always the "DC" bin
        
        # Scape currently blunders if the file contains nan or --, so only write valid numbers
        notmasked = ~Tdiode.mask & np.isfinite(Tdiode)
        _f, _T = np.compress(notmasked, freqs), np.compress(notmasked, Tdiode)
        if (rx_band.upper() == "U"): # Automatically skip low freq garbage
            _f, _T = _f[_f>540e6], _T[_f>540e6]
        
        if debug:
            plot_data(_f/1e6, _T, style='.', xtag="Frequency [MHz]", ytag="T_ND [K]", newfig=(pol==0))
        
        # Write CSV files
        #np.savetxt('%s/rx.%s.%s.%s.csv' % (output_dir, rx_band.lower(), rx_SN, "hv"[pol]), # TODO: use this rather than below
        #           np.c_[_f,_T], delimiter=",", fmt='%.2f')
        outfile = open('%s/rx.%s.%s.%s.csv' % (output_dir, rx_band.lower(), rx_SN, "hv"[pol]), 'w')
        outfile.write('#Noise Diode table based on measurements on %s\n# Frequency [Hz], Temperature [K]\n'%ant)
        if (_f[-1] < _f[0]): # unflip the flipped first nyquist ordering
            _f, _T = np.flipud(_f), np.flipud(_T)
        outfile.write(''.join(['%s, %s\n' % (entry[0], entry[1]) for entry in zip(_f,_T)]))
        outfile.close()


def load4hpbw(ds, savetofile=None, ch_res=16, cleanchans=None, jump_zone=0, cached=False, return_all=False, debug=2):
    """ Processes a raw dataset to determine the beam crossing time instant, as well as the half power crossing duration.
        If those results were previously saved to local disk, this function may be used to load it again.
        Note: This function does not modify the active selection filter of the raw dataset.
         
        @param ds: either a raw drift scan dataset (preferably from 'load_vis()') or an npz file name
        @param savetofile: npz filename to save the data to (default None)
        @param ch_res: > 0 to fit beams for every "ch_res" frequency bin or <=0 to fit band average only (default 16).
        @param cleanchans: clean channels to use to auto-detect and mask out "jumps" (default None).
        @param jump_zone: controls automatic excision of jumps over time, see 'fit_bm()' (default 0)
        @param cached: True to load from 'savetofile' if it exists (default False)
        @param return_all: True if ds is a raw dataset, to also return fitted (baseline,beam) as extra data (default False)
        @param debug: as for fit_bm()
        @return: (f [Hz], mu@f [seconds since ds.time_start], sigma@f [seconds], extra...). mu & sigma are masked arrays like 'fit_bm()'.
    """
    extra = []
    
    if hasattr(ds, "channel_freqs"): # A raw dataset (might still be cached)
        if not savetofile: # Default savetofile name
            savetofile = "%s_%d.npz" % (ds.name.split("/")[-1].split(".")[0], ch_res)
    
        if cached and savetofile and not return_all: # We don't cache the extras
            try:
                return load4hpbw(ds=savetofile)
            except:
                pass
        # Not cached, so do all the fitting work...
        
        # Only use drift scan section to prevent ND jumps from influencing fits, but don't use select()!
        time_mask = (ds.sensor["Antennas/array/activity"]=="track") & (ds.sensor["Observation/label"]=="drift")
        label = "%s - %s" % (ds.name.split("/")[-1].split()[0], ds.catalogue.targets[0].name)
        # For v4 datasets (using dask) we need to split [time,chans,...] as [time][chans,...]
        bl,mdl,sigma,mu = fit_bm(np.abs(ds.vis[:]), ch_res=ch_res, freqchans=cleanchans, timemask=time_mask, jump_zone=jump_zone, debug=debug, debug_label=label)
        sigma *= ds.dump_period # [dumps] -> [seconds]
        T0 = ds.timestamps[0] - float(ds.start_time)
        mu = T0 + mu*ds.dump_period # [dumps] -> [seconds since start]
        f = ds.channel_freqs
        
        if return_all: 
            extra = [bl, mdl]
        
        if savetofile:
            np.savez(savetofile, mu=mu.data, sigma=sigma.data, mask=mu.mask, f=f)
            data = np.load(savetofile, allow_pickle=True)
            mask, f = data['mask'], data['f']
            mu, sigma = np.ma.masked_array(data['mu'],mask), np.ma.masked_array(data['sigma'],mask)
            data.close()
    
    else: # Not a raw dataset so probably the results from a previous run on the raw data
        data = np.load(ds, allow_pickle=True)
        mask, f = data['mask'], data['f']
        mu, sigma = np.ma.masked_array(data['mu'],mask), np.ma.masked_array(data['sigma'],mask)
        data.close()
    
    return [f, mu, sigma] + extra


def fit_hpbw(f,mu,sigma, D, theta_src=0, fitchans=None, debug=True):
    """ Finds the best fit polynomial that describes the half power width over frequency. Includes both the beam and source widths!
        
        @param f,mu,sigma: as returned by 'load4hpbw()', or possibly only the set for a single product.
        @param D: the aperture diameter [m] to scale the fitted coefficients to.
        @param theta_src: the equivalent half-power width [rad] of the target (default 0)
        @param fitchans: selector to limit the frequency channels over which to fit the model (default None)
        @return: lambda f: hpw [rad]
    """
    theta_src = 0 if (theta_src is None) else theta_src
    # Basic model and constants
    hpbw = lambda f, p,q: ((p*(_c_/f)**q/D)**2 + theta_src**2)**.5 # rad
    omega_e = 2*np.pi/(24*60*60.) # rad/sec
    K = np.sqrt(8*np.log(2)) # hpbw = K*sigma
    
    if debug:
        plot_data(f/1e6, K*sigma*omega_e*180/np.pi, style=',', newfig=True, xtag="Frequency [MHz]", ytag="HPBW [deg]", y_lim='pct')
    
    # The specified data range
    sigma = np.atleast_2d(np.ma.array(sigma, copy=True))
    sigma[np.isnan(sigma)] = 0 # Avoid warnings in 'ssigma<_s' below if there are nan's
    N_prod = sigma.shape[1]
    fitchans = fitchans if fitchans else slice(None)
    ff, ssigma = f[fitchans], sigma[fitchans]
    # Mask out more data which is too far off the expected smooth curve
    _s = np.stack([smooth(sigma[:,p], sigma.shape[0]//50) for p in range(N_prod)], -1)[fitchans] # ~50 independent windows, balance 'noise' and edge effects
    ssigma.mask[ssigma<0.9*_s] = True; ssigma.mask[ssigma>1.1*_s] = True
    if debug:
        plot_data(ff/1e6, K*ssigma*omega_e*180/np.pi, style='.', label="Measured", newfig=False)
#         plot_data(ff/1e6, K*_s*omega_e*180/np.pi, style='k,', newfig=False)
    print("Fitting HPBW over %.f - %.f MHz assuming D=%.2f m"%(np.min(ff[~ssigma.mask[:,0]])/1e6, np.max(ff[~ssigma.mask[:,0]])/1e6, D))
    
    # lambda^1 model
    _p = sop.fmin_bfgs(lambda p: np.nansum((np.dstack([hpbw(ff,p[0],1)]*N_prod)-K*ssigma*omega_e)**2), [1], disp=False)
    _hpbw = hpbw(f,_p[0],1)
    print("Simultaneous fit to %d products: %g lambda^1 / %g"%(N_prod,_p[0],D))
    
    # lambda^n model
    __p = sop.fmin_bfgs(lambda p: np.nansum((np.dstack([hpbw(ff,p[0],p[1])]*N_prod)-K*ssigma*omega_e)**2), [1,1], disp=False)
    __hpbw = hpbw(f,*__p)
    print("Simultaneous fit to %d products: %g lambda^(%g) / %g"%(N_prod,__p[0],__p[1],D))

    if debug:
        plot_data(f/1e6, _hpbw*180/np.pi, label="$%.2f \lambda/%.3f$ [rad]"%(_p[0],D), newfig=False)
        plot_data(f/1e6, __hpbw*180/np.pi, label="$%.2f \lambda^{%.2f}/%.3f$ [rad]"%(__p[0],__p[1],D), newfig=False)
    return lambda f: hpbw(f,*__p)



def compare_FieldInAeTsys(freqs, SEFD_meas, SEFD_pred=None, y_lim='pct'):
    """ Plots the results in terms of Ae/Tsys.
    
        @param freqs, SEFD_meas, SEFD_pred: the leading output terms of get_SEFD_ND()
    """
    plot_data(freqs/1e6, 2*_kB_/1e-26 / SEFD_meas[:,0], label="Measured H", xtag="f [MHz]", ytag="$A_e/T_{sys}$ [m$^2$/K]")
    plot_data(freqs/1e6, 2*_kB_/1e-26 / SEFD_meas[:,1], newfig=False, label="Measured V", y_lim=y_lim)
    if (SEFD_pred is not None):
        plot_data(freqs/1e6, 2*_kB_/1e-26 / SEFD_pred[:,0], newfig=False, label="Predicted H")
        plot_data(freqs/1e6, 2*_kB_/1e-26 / SEFD_pred[:,1], newfig=False, label="Predicted V", y_lim=y_lim)


def compare_FieldToSpec(freqs, SEFD_meas, SEFD_pred, *args):
    """ Compares the measured results to the MeerKAT allocated specifications, in Ae/Tsys.
    
        @param ...: the output of get_SEFD_ND()
    """
    D = 13.5
    Nant = 64.
    
    spec_Ae_Tsys = 0*freqs
    spec_Ae_Tsys[freqs<1420e6] = (Nant * np.pi*D**2/4.) / 42 # [R.T.P095] == 220
    spec_Ae_Tsys[freqs>=1420e6] = (Nant * np.pi*D**2/4.) / 46 # [R.T.P.096] == 200
    
    compare_FieldInAeTsys(freqs, SEFD_meas, SEFD_pred)
    plot_data(freqs/1e6, spec_Ae_Tsys/Nant, newfig=False, label="\"220-200 m$^2$/K\" at Telescope PDR", y_lim='pct')
    plot_data(freqs/1e6, np.linspace(275,410,len(freqs))/Nant, newfig=False, label="\"275-410 m$^2$/K\" at Receivers CDR")
    
    compare_FieldInAeTsys(freqs, SEFD_meas*Nant, SEFD_pred*Nant)
    plot_data(freqs/1e6, spec_Ae_Tsys, newfig=False, label="220-200 m$^2$/K at Telescope PDR", y_lim='pct')
    plot_data(freqs/1e6, np.linspace(275,410,len(freqs)), newfig=False, label="275-410 m$^2$/K at Receivers CDR")


if __name__ == "__main__":
    import optparse
    parser = optparse.OptionParser(usage="%prog [options] <data file>",
                                   description="Processes a drift scan dataset, using aperture efficiency model from records"
                                               " (if not available for a band, assumes some nominal default value)."
                                               "Results are summarised in figures and processing log in PDF report.")
    parser.add_option('-a', '--ant', type='int', default=0,
                      help="Antenna numerical sequence as listed in the dataset - NOT receptor ID (default %default).")
    parser.add_option('-t', '--target', type='string', default=None,
                      help="Target name to either look up flux model in katsemodels.py, or from catalogue.")
    parser.add_option('-x', '--flux-key', type='string', default=None,
                      help="Identify the flux model to use for the source, or None if the target is loaded from the catalogue (default %default)")
    parser.add_option('-b', '--hpbw', type='string', default="lambda f: 1.22*(_c_/f)/13.965", # "Nominal best fit" for MeerKAT UHF & L-band
                      help="None, or a function that defines the half power beamwidth in radians, like 'lambda f_Hz: rad' (default %default)")
    parser.add_option('--rfi-mask', type='string', default=None,
                      help="RFI frequency mask as a list of tuples in Hz, e.g. [(925e6,965e6)].")
    parser.add_option('--fit-freq', type='string', default="[580e6,720e6]",
                      help="Range of frequencies in Hz over which to fit baselines (default %default)")
    parser.add_option('--strict', action='store_true', default=False,
                      help="Strictly avoid noise diode unless tracking the target (default %default).")
    
    (opts, args) = parser.parse_args()
    opts.hpbw = eval(opts.hpbw)
    freqmask = eval(opts.rfi_mask)
    fitfreqrange = eval(opts.fit_freq)
    
    result = analyse(args[0], opts.ant, opts.target, opts.flux_key, HPBW=opts.hpbw, fitfreqrange=fitfreqrange, freqmask=freqmask, strict=opts.strict, saveroot=".", makepdf=True)
    