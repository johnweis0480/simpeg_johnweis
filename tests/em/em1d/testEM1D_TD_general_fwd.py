import unittest
from SimPEG import maps
from SimPEG.utils import mkvc
import matplotlib.pyplot as plt
import SimPEG.electromagnetics.time_domain as tdem
import SimPEG.electromagnetics.time_domain_1d as em1d
from SimPEG.electromagnetics.time_domain_1d.supporting_functions.waveform_functions import *
from SimPEG.electromagnetics.analytics.em1d_analytics import *
import numpy as np
from scipy import io
from scipy.interpolate import interp1d


class EM1D_TD_CircularLoop_FwdProblemTests(unittest.TestCase):

    def setUp(self):
        
        nearthick = np.logspace(-1, 1, 5)
        deepthick = np.logspace(1, 2, 10)
        thicknesses = np.r_[nearthick, deepthick]
        topo = np.r_[0., 0., 100.]

        source_location = np.array([0., 0., 100.+1e-5])
        receiver_locations = np.array([[0., 0., 100.+1e-5]])
        receiver_orientation = "z"  # "x", "y" or "z"
        times = np.logspace(-5, -2, 31)
        radius = 20.

        # Waveform
        waveform_times = np.r_[-np.logspace(-2, -5, 31), 0.]
        waveform_current = triangular_waveform_current(
            waveform_times, -0.01, -0.005, 0., 1.
        )        
        
        waveform = tdem.sources.RawWaveform(
                waveform_times=waveform_times, waveform_current=waveform_current,
                n_pulse = 1, base_frequency = 25.,  high_cut_frequency=210*1e3
        )

        # Receiver list

        # Define receivers at each location.
        b_receiver = tdem.receivers.PointMagneticFluxDensity(
            receiver_locations, times, receiver_orientation
        )
        dbzdt_receiver = tdem.receivers.PointMagneticFluxTimeDerivative(
            receiver_locations, times, receiver_orientation
        )
        receivers_list = [
            b_receiver, dbzdt_receiver
        ]  # Make a list containing all receivers even if just one

        # Must define the transmitter properties and associated receivers
        source_list = [
            tdem.sources.CircularLoop(
                receivers_list,
                location=source_location,
                waveform=waveform,
                radius=radius,
                i_sounding=0
            )
        ]

        survey = tdem.Survey(source_list)
        
        sigma = 1e-2

        self.topo = topo
        self.survey = survey
        self.showIt = False
        self.sigma = sigma
        self.times = times
        self.thicknesses = thicknesses
        self.nlayers = len(thicknesses)+1
        self.a = radius
        

    def test_em1dtd_circular_loop_single_pulse(self):
        
        sigma_map = maps.ExpMap(nP=self.nlayers)
        sim = em1d.simulation.EM1DTMSimulation(
            survey=self.survey, thicknesses=self.thicknesses,
            sigmaMap=sigma_map, topo=self.topo
        )
        
        m_1D = np.log(np.ones(self.nlayers)*self.sigma)
#        f = sim.compute_integral(m_1D)
        d = sim.dpred(m_1D)
        bz = d[0:len(self.times)]
        dbdt = d[len(self.times):]

        def step_func_Bzt(times):
            return Bz_horizontal_circular_loop(
                self.a, times, self.sigma
            )

        bz_analytic = piecewise_ramp(
            step_func_Bzt, self.times,
            sim.survey.source_list[0].waveform.waveform_times,
            sim.survey.source_list[0].waveform.waveform_current
        )

        if self.showIt:
            plt.subplot(121)
            plt.loglog(self.times, bz, 'b*')
            plt.loglog(self.times, bz_analytic, 'b')
            plt.subplot(122)
            plt.loglog(
                self.times, abs((bz-bz_analytic)/bz_analytic), 'r:'
            )
            plt.show()
        
        err = np.linalg.norm(bz-bz_analytic)/np.linalg.norm(bz_analytic)
        print ('Bz error = ', err)
        self.assertTrue(err < 6e-2)

        def step_func_dBzdt(times):
            return dBzdt_horizontal_circular_loop(
                self.a, times, self.sigma
            )

        dbdt_analytic = piecewise_ramp(
            step_func_dBzdt, self.times,
            sim.survey.source_list[0].waveform.waveform_times,
            sim.survey.source_list[0].waveform.waveform_current
        )
        
        if self.showIt:
            plt.subplot(121)
            plt.loglog(self.times, abs(dbdt), 'b*')
            plt.loglog(
                self.times,
                abs(dbdt_analytic), 'b'
            )
            plt.subplot(122)
            plt.loglog(
                self.times,
                abs((dbdt-dbdt_analytic)/dbdt_analytic), 'r:'
            )
            plt.show()

        err = (
            np.linalg.norm(dbdt-dbdt_analytic)/
            np.linalg.norm(dbdt_analytic)
        )

        print ('dBzdt error = ', err)
        self.assertTrue(err < 6e-2)

        print ("EM1DTD-CirculurLoop-general for real conductivity works")


class EM1D_TD_MagDipole_FwdProblemTests(unittest.TestCase):

    def setUp(self):
        
        nearthick = np.logspace(-1, 1, 5)
        deepthick = np.logspace(1, 2, 10)
        thicknesses = np.r_[nearthick, deepthick]
        topo = np.r_[0., 0., 100.]

        source_location = np.array([0., 0., 100.+1e-5])
        receiver_locations = np.array([[10., 0., 100.+1e-5]])
        receiver_orientation = "z"  # "x", "y" or "z"
        times = np.logspace(-5, -2, 31)
        radius = 20.

        # Waveform
        waveform_times = np.r_[-np.logspace(-2, -5, 31), 0.]
        waveform_current = triangular_waveform_current(
            waveform_times, -0.01, -0.005, 0., 1.
        )        
        
        waveform = tdem.sources.RawWaveform(
                waveform_times=waveform_times, waveform_current=waveform_current,
                n_pulse = 1, base_frequency = 25.,  high_cut_frequency=210*1e3
        )

        # Receiver list

        # Define receivers at each location.
        b_receiver = tdem.receivers.PointMagneticFluxDensity(
            receiver_locations, times, receiver_orientation
        )
        dbzdt_receiver = tdem.receivers.PointMagneticFluxTimeDerivative(
            receiver_locations, times, receiver_orientation
        )
        receivers_list = [
            b_receiver, dbzdt_receiver
        ]  # Make a list containing all receivers even if just one

        # Must define the transmitter properties and associated receivers
        source_list = [
            tdem.sources.MagDipole(
                receivers_list,
                location=source_location,
                waveform=waveform,
                i_sounding=0
            )
        ]

        survey = tdem.Survey(source_list)
        
        sigma = 1e-2

        self.topo = topo
        self.survey = survey
        self.showIt = False
        self.sigma = sigma
        self.times = times
        self.thicknesses = thicknesses
        self.nlayers = len(thicknesses)+1
        self.a = radius
        

    def test_em1dtd_mag_dipole_single_pulse(self):
        
        sigma_map = maps.ExpMap(nP=self.nlayers)
        sim = em1d.simulation.EM1DTMSimulation(
            survey=self.survey, thicknesses=self.thicknesses,
            sigmaMap=sigma_map, topo=self.topo
        )
        
        m_1D = np.log(np.ones(self.nlayers)*self.sigma)
#        f = sim.compute_integral(m_1D)
        d = sim.dpred(m_1D)
        bz = d[0:len(self.times)]
        dbdt = d[len(self.times):]

        def step_func_Bzt(times):
            return Bz_vertical_magnetic_dipole(
                10., times, self.sigma
            )

        bz_analytic = piecewise_ramp(
            step_func_Bzt, self.times,
            sim.survey.source_list[0].waveform.waveform_times,
            sim.survey.source_list[0].waveform.waveform_current
        )

        if self.showIt:
            plt.subplot(121)
            plt.loglog(self.times, bz, 'b*')
            plt.loglog(self.times, bz_analytic, 'b')
            plt.subplot(122)
            plt.loglog(
                self.times, abs((bz-bz_analytic)/bz_analytic), 'r:'
            )
            plt.show()
        
        err = np.linalg.norm(bz-bz_analytic)/np.linalg.norm(bz_analytic)
        print ('Bz error = ', err)
        self.assertTrue(err < 6e-2)

        print ("EM1DTD-CirculurLoop-general for real conductivity works")

if __name__ == '__main__':
    unittest.main()