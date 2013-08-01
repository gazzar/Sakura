#!/usr/bin/env python

import os
import numpy as np
from xmap_netcdf_reader import DetectorData
import readMDA

from utils import memoize

#
# set up a CLASS for detector pixels
#


class Pixel(object):
    """Class to describe a detector element and its 'contents'
    There will be 100 instances later to represent the 100-element detector.

    """
    __pxId = 0    # class variable for generating a unique pixel id on construction

    def __init__(self, detector):
        self.detector = detector    # set the "parent" detector
        self.detector_data = detector.detector_data
        # assign and increment unique id
        self.pixNum = Pixel.__pxId
        Pixel.__pxId += 1
        self.row, self.col = divmod(self.pixNum, self.detector.rows)
        self.roiCorr = None
        self.roiCorrNorm = None
        self.weightedSpec = None
        self.weightFactor = None
        self.chi = None
        self.tau = None

    # expose the DetectorData spectrum(), statistic(), buffer_header_item() and
    #    pixel_header_mode1_item() methods here
    def spectrum(self, *args, **kwargs):
        return self.detector_data.spectrum(*args, **kwargs)

    @memoize
    def statistic(self, *args, **kwargs):
        return self.detector_data.statistic(*args, **kwargs)

    def pixel_header_mode1_item(self, *args, **kwargs):
        return self.detector_data.pixel_header_mode1_item(*args, **kwargs)

    def buffer_header_item(self, *args, **kwargs):
        return self.detector_data.buffer_header_item(*args, **kwargs)

    def _GetSpectrumROI(self, pixel_step, low, high):
        """ low and high are semi-open range indices
        """
        spectrum = self.detector_data.spectrum(pixel_step, self.row, self.col)
        return spectrum[low:high]

    @memoize
    def _roi(self, roi_low, roi_high):
        return np.array([self._GetSpectrumROI(step, roi_low, roi_high).sum()
                         for step in self.detector.steprange])
    @property
    @memoize
    def fpeaks(self):
        return np.array([self.statistic(step, self.row, self.col, 'realtime')
                        for step in self.detector.steprange])

    @property
    @memoize
    def speaks(self):
        return np.array([self.statistic(step, self.row, self.col, 'livetime')
                        for step in self.detector.steprange])

    @property
    def roi(self):
        return self._roi(self.detector.roi_low, self.detector.roi_high)

#     def NormT(self, tsample) :
#         # normalise all raw data (FastPeaks, SlowPeaks, ROI) to sampling time tsample
#         self.fpeaks = np.divide(np.ndarray.flatten(self.fpeaks), tsample)
#         self.speaks = np.divide(np.ndarray.flatten(self.speaks), tsample)
#         self.roi = np.divide(np.ndarray.flatten(self.roi), tsample)

    def GetDead(self, fpeaks, speaks):
        # get detector dead time parameter "tau"
        try:
            self.tau = fpeaks / speaks
        except ZeroDivisionError:
            self.tau = fpeaks

    def DeadCorr(self, tau, roi):
        # apply dead time parameter
        self.roiCorr = roi * tau

    def NormI0(self, I0):
        # normalise data to incoming intensity (I0)
        self.roiCorrNorm = self.roiCorr / I0

    def WeightSpectrum(self):
        # apply weight factor which was derived from relative edge step
        self.weightedSpec = self.weightFactor * self.roiCorr


class Detector(list):
    """A detector class that acts like a list container for accessing pixel elements
    http://stackoverflow.com/questions/921334/how-to-use-classes-derived-from-pythons-list-class
    http://docs.python.org/2/reference/datamodel.html#emulating-container-types
    It needs to store a reference to a DetectorData instance and contain all the pixel
    objects.

    """
    def __init__(self, detector_data):
        """detector is a DetectorData instance
        """
        self.detector_data = detector_data
        self.rows, self.cols = detector_data.shape
        detector_size = self.rows * self.cols
        self.det = [Pixel(self) for _ in range(detector_size)]
        self.steprange = None           # Holds an iterable containing the energy/mca/pixel-steps
        self.iter_pointer = 0
        # The roi limits will apply to all contained detector elements - the pixel
        # element objects keep a reference to this object so they can access these fields
        self.roi_low = None
        self.roi_high = None

    # Implement __getitem__, __setitem__, __iter__ and __len__ methods to implement
    # list-like behaviour
    def __getitem__(self, key):
        return self.det[key]

    def __setitem__(self, key, value):
        self.det[key] = value

    def __iter__(self):
        # from http://stackoverflow.com/questions/4019971/how-to-implement-iter-self-for-a-container-object-python
        if hasattr(self.det[0], "__iter__"):
            return self.det[0].__iter__()
        return self.det.__iter__()

    def __len__(self):
        return len(self.det)

    def __str__(self):
        return '<{}.{} object at {}>'.format(
            self.__class__.__module__,
            self.__class__.__name__,
            hex(id(self))
        )

    # Methods for our container class additional to those required for list-like behaviour
    def set_roi_limits(self, low, high):
        """Set the roi limits for the detector."""
        self.roi_low = low
        self.roi_high = high


def getExtraPV(mda_list, pv):
    """Return the PV value using the PV part of an IOC:PV id for matching
    e.g. getExtraPV(mda_list, 'CUR_TIME_STAMP') will match SR12ID01MC01:CUR_TIME_STAMP
    then use this as the key to retrieve the corresponding value from the dictionary
    inside mda_list

    Arguments:
    mda_list - the list returned by Tim Mooney's readMDA
    pv - a string, e.g. 'CUR_TIME_STAMP'

    Returns:
    The matching PV keyname in the dict inside mda_list

    """
    extra_pvs = mda_list[0]         # get the first list entry - a dict containing the PVs 
    keys = {i.split(':')[-1]: i for i in extra_pvs}
    return extra_pvs[keys[pv]]


def highest_available_scandata(detector, scanSize):
    """Step through the netCDF files to verify we have all the fluoresence data available
    Arguments:
    detector - detector instance reference
    scanSize - expected number of pixel steps, likely based on the number written in the
               mda file

    Returns:
    index of the highest pixel step read (0-based)

    Exceptions:
    Raises IndexError if expected data is unavailable

    """
    for i in range(scanSize):
        try:
            detector.det[0].pixel_header_mode1_item(i, 0, 0, 'tag0', check_validity=False)
        except IndexError:
            print 'netCDF data truncated'
    return i + 1


def getData(fname):
    """Extract data from mda-ASCII file and distribute into Pixel objects

    Returns: XAS scan data in a detector 'object' (variable "det")
            energy axis, transmission data array and detector filled with fluo data
    (detector object simply full of '0' values if no fluorescence data available)
    Transmission data in "trans" comprises encoder_E, I0/1/2, sample_time, encoder_angle
    !! I0/1/2  are already normalised to t !!

    """
    # get the path to the netCDF files from the mda file
    mda = readMDA.readMDA(fname, verbose=False)
    timestamp = getExtraPV(mda, 'CUR_TIME_STAMP')[-1][0]
    netcdf_directory = os.path.join(os.path.dirname(os.path.abspath(fname)),
                                    'out_{}'.format(timestamp))

    scanData = mda[1]
    scanSize = scanData.npts

    # create and set the reader for the fluorescence detector
    detector_data = DetectorData(shape=(10, 10), pixelsteps_per_buffer=1,
        buffers_per_file=1, dirpaths=netcdf_directory,
        filepattern='ioc5[3-4]_([0-9]*)\.nc', mca_bins=2048, first_file_n=1)

    detector = Detector(detector_data)

    # set scanSize according to the netCDF data that was available
    scanSize = highest_available_scandata(detector, scanSize)
    detector.steprange = range(scanSize)

    # TODO: get rid of the next 2 lines
    detector.set_roi_limits(600, 800)   # start with this roi range
#     scanSize = 20
#     detector.steprange = range(129,158)      # start with first 10 energy points

    # read transmission data
    pvColumnNames = ['EncEnergy:ActPos', 'scaler1:S2C', 'scaler1:S3C',
                     'scaler1:S4C', 'scaler1.T', 'EncAngle:ActPos']

    trans = np.empty((len(pvColumnNames), scanSize))
    for series in scanData.d:
        try:
            tag = ':'.join(series.name.split(':')[1:])  # use the PV part after the IOC id
            if tag in pvColumnNames:
                trans[pvColumnNames.index(tag)] = series.data[:scanSize]
        except Exception as e:
            print e
            print 'missing PV ' + tag

    ts = trans[pvColumnNames.index('scaler1.T')]    # get the sample time
    e = trans[pvColumnNames.index('EncEnergy:ActPos')] * 1000.0  # Energy axis (in eV !!)

    # normalise I0, I1, I2 to sample_time ts (use string "scaler1:" as identifier)
    for i, name in enumerate(pvColumnNames):
        if name.startswith('scaler1:'):
            trans[i] = trans[i] / ts

    ## call dead time correction later; should be performed only on
    ## good spectra ("goodPixels") as bad spectra can contain zeros which
    ## this function would divide by
    ## call is via function:  detDeadCorr(det, goodPixels)

    return e, trans, detector


if __name__ == '__main__':
    pass
