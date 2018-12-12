""" This module contains the classes for all beamline elements. """

import time
from multiprocessing import Pool, cpu_count
from functools import partial

import numpy as np
import scipy.constants as ct
import aptools.plasma_accel.general_equations as ge

from wake_t.particle_tracking import runge_kutta_4
from wake_t.wakefields import *
from wake_t.driver_witness import ParticleBunch


class PlasmaStage(object):

    """ Defines a plasma stage. """

    def __init__(self, n_p, length):
        """
        Initialize plasma stage.

        Parameters:
        -----------
        n_p : float
            Plasma density in units of cm^{-3}.

        length : float
            Length of the plasma stage in cm.

        """
        self.n_p = n_p
        self.length = length

    def get_matched_beta(self, mode, ene, xi=None, foc_strength=None,
                         laser=None):
        """
        Calculate the matched beta function at the plasma for a beam energy.

        Parameters:
        -----------
        mode : string
            Mode used to calculate fields. Possible values are 'Blowout',
            'CustomBlowout', 'FromGivenFields'(deprecated) and 'Linear'.

        ene : float
            Mean beam energy in non-dimensional units (beta*gamma).

        xi : float
            Longitudinal position of the bunch center in the comoving frame.
            Only used if mode='Linear'.

        foc_strength : float
            Focusing gradient in the plasma in units of T/m. Onnly used for
            modes 'CustomBlowout' and 'FromGivenFields'

        laser : LaserPulse
            Laser used in the plasma stage. Only used if mode='Linear'.

        """
        if mode == "Blowout":
            return ge.matched_plasma_beta_function(ene, n_p=self.n_p,
                                                   regime='Blowout')

        elif mode in ["CustomBlowout", "FromGivenFields"]:
            return ge.matched_plasma_beta_function(ene, k_x=foc_strength)

        elif mode == "Linear":
            dist_l_b = -(laser.get_lon_center()-xi)
            return ge.matched_plasma_beta_function(
                ene, n_p=self.n_p, regime='Blowout', dist_from_driver=dist_l_b,
                a_0=laser.a_0, w_0=laser.w_0)

    def _gamma(self, px, py, pz):
        return np.sqrt(1 + np.square(px) + np.square(py) + np.square(pz))
    
    def track_beam_numerically_RK_parallel(
            self, laser, beam, mode, steps, simulation_code=None,
            simulation_path=None, time_step=None, auto_update_fields=False,
            laser_pos_in_pic_code=None, lon_field=None, lon_field_slope=None,
            foc_strength=None, filter_fields=False, filter_sigma=20,
            n_proc=None):
        """
        Track the beam through the plasma using a 4th order Runge-Kutta method.
        
        Parameters:
        -----------
        laser : LaserPulse
            Laser used in the plasma stage.

        beam : ParticleBunch
            Particle bunch to track.

        mode : string
            Mode used to determine the wakefields. Possible values are 
            'Blowout', 'CustomBlowout', 'FromPICCode'.

        steps : int
            Number of steps in which output should be given.

        simulation_code : string
            Name of the simulation code from which fields should be read. Only
            used if mode='FromPICCode'.

        simulation_path : string
            Path to the simulation folder where the fields to read are located.
            Only used if mode='FromPICCode'.

        time_step : int
            Time step at which the fields should be read.

        auto_update_fields : bool
            If True, new fields will be read from the simulation folder
            automatically as the particles travel through the plasma. The first
            time step will be time_step, and new ones will be loaded as the
            time of flight of the bunch reaches the next time step available in
            the simulation folder.

        laser_pos_in_pic_code : float (deprecated)
            Position of the laser pulse center in the comoving frame in the pic
            code simulation.

        lon_field : float
            Value of the longitudinal electric field at the bunch center at the
            beginning of the tracking in units of V/m. Only used if
            mode='CustomBlowout'.

        lon_field_slope : float
            Value of the longitudinal electric field slope along z at the bunch
            center at the beginning of the tracking in units of V/m^2. Only
            used if mode='CustomBlowout'.

        foc_strength : float
            Value of the focusing gradient along the bunch in units of T/m. 
            Only used if mode='CustomBlowout'.

        filter_fields : bool
            If true, a Gaussian filter is applied to smooth the wakefields.
            This can be useful to remove noise. Only used if
            mode='FromPICCode'.

        filter_sigma : float
            Sigma to be used by the Gaussian filter. 

        n_proc : int
            Number of processes to run in parallel. If None, this will equal
            the number of physical cores.

        Returns:
        --------
        A list of size 'steps' containing the beam distribution at each step.

        """
        if mode == "Blowout":
            WF = BlowoutWakefield(self.n_p, laser)
        if mode == "CustomBlowout":
            WF = CustomBlowoutWakefield(
                self.n_p, laser, np.average(beam.xi, weights=beam.q), 
                lon_field, lon_field_slope, foc_strength)
        elif mode == "FromPICCode":
            WF = WakefieldFromPICSimulation(
                simulation_code, simulation_path, laser, time_step, self.n_p,
                filter_fields, filter_sigma)
        # Get 6D matrix
        mat = beam.get_6D_matrix()
        # Plasma length in time
        t_final = self.length/ct.c
        t_step = t_final/steps
        dt = self._get_optimized_dt(beam, WF)
        iterations = int(t_final/dt)
        # force at least 1 iteration per step
        it_per_step = max(int(iterations/steps), 1)
        iterations = it_per_step*steps
        dt_adjusted = t_final/iterations
        # initialize list to store the distribution at each step
        beam_list = list()
        # get start time
        start = time.time()
        if n_proc is None:
            num_proc = cpu_count()
        else:
            num_proc = n_proc
        num_part = mat.shape[1]
        part_per_proc = int(np.ceil(num_part/num_proc))
        process_pool = Pool(num_proc)
        t_s = 0
        matrix_list = list()
        # start computaton
        try:
            for p in np.arange(num_proc):
                matrix_list.append(mat[:,p*part_per_proc:(p+1)*part_per_proc])

            for s in np.arange(steps):
                print(s)
                if auto_update_fields:
                    WF.check_if_update_fields(s*t_step)
                partial_solver = partial(
                    runge_kutta_4, WF=WF, dt=dt_adjusted,
                    iterations=it_per_step, t0=s*t_step)
                matrix_list = process_pool.map(partial_solver, matrix_list)
                beam_matrix = np.concatenate(matrix_list, axis=1)
                x, px, y, py, xi, pz = beam_matrix
                new_prop_dist = beam.prop_distance + (s+1)*t_step*ct.c
                beam_list.append(
                    ParticleBunch(beam.q, x, y, xi, px, py, pz,
                                  prop_distance=new_prop_dist)
                    )
        finally:
            process_pool.close()
            process_pool.join()
        # print computing time
        end = time.time()
        print("Done ({} seconds)".format(end-start))
        # update beam data
        last_beam = beam_list[-1]
        beam.set_phase_space(last_beam.x, last_beam.y, last_beam.xi,
                             last_beam.px, last_beam.py, last_beam.pz)
        beam.increase_prop_distance(self.length)
        return beam_list
    
    def _get_optimized_dt(self, beam, WF):
        """ Get optimized time step """ 
        gamma = self._gamma(beam.px, beam.py, beam.pz)
        Kx = WF.Kx(
            beam.x, beam.y, beam.xi, beam.px, beam.py, beam.pz, gamma, 0)
        mean_Kx = np.average(Kx, weights=beam.q)
        mean_gamma = np.average(gamma, weights=beam.q)
        w_x = np.sqrt(ct.e*ct.c/ct.m_e * mean_Kx/mean_gamma)
        T_x = 1/w_x
        dt = 0.1*T_x
        return dt

    def track_beam_analytically(self, laser, beam, mode, steps,
                                    simulation_path=None, time_step=None,
                                    laser_pos_in_osiris=None, lon_field=None,
                                    lon_field_slope=None, foc_strength=None,
                                    foc_strength_slope=None):
        """Tracks the beam through the plasma and returns the final phase space"""
        # Main laser quantities
        l_c = laser.xi_c
        v_w = laser.get_group_velocity(self.n_p)*ct.c
        w_0_l = laser.w_0

        # Main beam quantities [SI units]
        x_0 = beam.x
        y_0 = beam.y
        xi_0 = beam.xi
        px_0 = beam.px * ct.m_e * ct.c
        py_0 = beam.py * ct.m_e * ct.c
        pz_0 = beam.pz * ct.m_e * ct.c

        # Distance between laser and beam particle
        dist_l_b = -(l_c-xi_0)

        # Plasma length in time
        t_final = self.length/ct.c

        # Fields
        if mode == "Blowout":
            """Bubble center is assumed at lambda/2"""
            w_p = np.sqrt(self.n_p*1e6*ct.e**2/(ct.m_e*ct.epsilon_0))
            l_p = 2*np.pi*ct.c/w_p
            E_p = -w_p**2/(2*ct.c)
            K = w_p**2/2
            E = E_p*(l_p/2+dist_l_b)

        elif mode == "CustomBlowout":
            E_p = -lon_field_slope*ct.e/(ct.m_e*ct.c)
            K = foc_strength*ct.c*ct.e/ct.m_e
            E = -lon_field*ct.e/(ct.m_e*ct.c) + E_p*(xi_0 - np.mean(xi_0))

        elif mode == "Linear":
            a0 = laser.a_0
            n_p_SI = self.n_p*1e6
            w_p = np.sqrt(n_p_SI*ct.e**2/(ct.m_e*ct.epsilon_0))
            k_p = w_p/ct.c
            E0 = ct.m_e*ct.c*w_p/ct.e
            K = (8*np.pi/np.e)**(1/4)*a0/(k_p*w_0_l)

            E_z = E0*np.sqrt(np.pi/(2*np.e))*a0**2*np.cos(k_p*(dist_l_b))
            E_z_p = -E0*np.sqrt(np.pi/(2*np.e))*a0**2*k_p*np.sin(k_p*(dist_l_b))
            g_x = -E0*K**2*k_p*np.sin(k_p*dist_l_b)/ct.c
            g_x_slope = -E0*K**2*k_p**2*np.cos(k_p*dist_l_b)/ct.c

            E = -ct.e/(ct.m_e*ct.c)*E_z
            E_p = -ct.e/(ct.m_e*ct.c)*E_z_p
            K = g_x*ct.c*ct.e/ct.m_e

        elif mode == "LinearAlberto":
            a0 = laser.a_0
            n_p_SI = self.n_p*1e6
            w_p = np.sqrt(n_p_SI*ct.e**2/(ct.m_e*ct.epsilon_0))
            k_p = w_p/ct.c
            E0 = ct.m_e*ct.c*w_p/ct.e

            nb0 = a0**2/2
            L  = np.sqrt(2)
            sz = L/np.sqrt(2)
            sx = w_0_l/2

            E_z = E0 * nb0 * np.sqrt(2*np.pi) * sz * np.exp(-(sz)**2/2) * np.cos(k_p*dist_l_b)
            E_z_p = - E0 * nb0 * np.sqrt(2*np.pi) * sz * np.exp(-(sz)**2/2) *k_p*np.sin(k_p*(dist_l_b)) # [V/m^2]
            g_x = -nb0 * np.sqrt(2*np.pi) * sz * np.exp(-(sz)**2/2) * ( 1/ (k_p*sx)**2) * np.sin(k_p*(dist_l_b)) * k_p*E0/ct.c

            E = -ct.e/(ct.m_e*ct.c)*E_z
            E_p = -ct.e/(ct.m_e*ct.c)*E_z_p
            K = g_x*ct.c*ct.e/ct.m_e

        elif mode == "FromOsiris2D":
            (E_z, E_z_p, g_x) = self.get_fields_from_osiris_2D(simulation_path, time_step, laser, laser_pos_in_osiris, dist_l_b)
            E = -ct.e/(ct.m_e*ct.c)*E_z
            E_p = -ct.e/(ct.m_e*ct.c)*E_z_p
            K = g_x*ct.c*ct.e/ct.m_e

        elif mode == "FromOsiris3D":
            (E_z, E_z_p, g_x) = self.get_fields_from_osiris_3D(simulation_path, time_step, laser, laser_pos_in_osiris, dist_l_b)
            E = -ct.e/(ct.m_e*ct.c)*E_z
            E_p = -ct.e/(ct.m_e*ct.c)*E_z_p
            K = g_x*ct.c*ct.e/ct.m_e
        

        # Some initial values
        p_0 = np.sqrt(np.square(px_0) + np.square(py_0) + np.square(pz_0))
        g_0 = np.sqrt(np.square(p_0*ct.c) + (0.511*1e6*ct.e)**2)/(0.511*1e6*ct.e) # gamma rel.
        w_0 = np.sqrt(K/g_0)

        # Initial velocities
        v_x_0 = px_0/(ct.m_e*g_0)
        v_y_0 = py_0/(ct.m_e*g_0)

        # calculate oscillation amplitude
        A_x = np.sqrt(x_0**2+v_x_0**2/w_0**2)
        A_y = np.sqrt(y_0**2+v_y_0**2/w_0**2)

        # initial phase (x)
        sn_x = -v_x_0/(A_x*w_0)
        cs_x = x_0/A_x
        phi_x_0 = np.arctan2(sn_x, cs_x)

        # initial phase (y)
        sn_y = -v_y_0/(A_y*w_0)
        cs_y = y_0/A_y
        phi_y_0 = np.arctan2(sn_y, cs_y)

        # track beam in steps
        #print("Tracking plasma stage in {} steps...   ".format(steps))
        start = time.time()
        p = Pool(cpu_count())
        t = t_final/steps*(np.arange(steps)+1)
        part = partial(self._get_beam_at_specified_time_step_from_paper_final, beam=beam, g_0=g_0, w_0=w_0, xi_0=xi_0, A_x=A_x, A_y=A_y, phi_x_0=phi_x_0, phi_y_0=phi_y_0, E=E, E_p=E_p, v_w=v_w, K=K)
        beam_steps_list = p.map(part, t)
        end = time.time()
        print("Done ({} seconds)".format(end-start))

        # update beam data
        last_beam = beam_steps_list[-1]
        beam.set_phase_space(last_beam.x, last_beam.y, last_beam.xi, last_beam.px, last_beam.py, last_beam.pz)
        beam.increase_prop_distance(self.length)

        # update laser data
        laser.increase_prop_distance(self.length)
        laser.xi_c = laser.xi_c + (v_w-ct.c)*t_final

        # return steps
        return beam_steps_list

    def _get_beam_at_specified_time_step_from_paper_final(self, t, beam, g_0, w_0, xi_0, A_x, A_y, phi_x_0, phi_y_0, E, E_p, v_w, K):
        # Start calculation
        # print(np.mean(g_0))
        G = 1 + E/g_0*t
        phi = 2*np.sqrt(K*g_0)/E*(G**(1/2) - 1)
        A_0 = np.sqrt(A_x**2 + A_y**2)

        x = A_x*G**(-1/4)*np.cos(phi + phi_x_0)
        v_x = -w_0*A_x*G**(-3/4)*np.sin(phi + phi_x_0)
        p_x = G*g_0*v_x/ct.c # [m_e *c]

        y = A_y*G**(-1/4)*np.cos(phi + phi_y_0)
        v_y = -w_0*A_y*G**(-3/4)*np.sin(phi + phi_y_0)
        p_y = G*g_0*v_y/ct.c # [m_e *c]

        delta_xi = ct.c/(2*E*g_0)*(G**(-1)- 1) + A_0**2*K/(2*ct.c*E)*(G**(-1/2) - 1)
        xi = xi_0 + delta_xi

        delta_xi_max = -1/(2*E)*(ct.c/g_0 + A_0**2*K/ct.c)
        
        g = g_0 + E*t + E_p*delta_xi_max*t +E_p/2*(ct.c-v_w)*t**2 + ct.c*E_p/(2*E**2)*np.log(G) +E_p*A_0**2*K*g_0/(ct.c*E**2)*(G**(1/2) - 1)
        p_z = np.sqrt(g**2-p_x**2-p_y**2) # [m_e *c]

        beam_step = ParticleBunch(beam.q, x, y, xi, p_x, p_y, p_z, prop_distance=beam.prop_distance+t*ct.c)

        return beam_step



class PlasmaRamp(object):

    """Defines a plasma ramp."""

    def __init__(self, length, plasma_dens_down, plasma_dens_top,
                 position_down=None, ramp_type='upramp',
                 profile='inverse square'):
        """
        Initialize plasma ramp.

        Parameters:
        -----------
        length : float
            Length of the plasma stage in cm.
            
        plasma_dens_down : float
            Plasma density at the position 'position_down' in units of
            cm^{-3}.

        plasma_dens_top : float
            Plasma density at the beginning (end) of the downramp (upramp) in
            units of cm^{-3}.

        position_down : float
            Position where the plasma density will be equal to 
            'plasma_dens_down' measured from the beginning (end) of the 
            downramp (upramp).

        """
        self.length = length
        self.plasma_dens_down = plasma_dens_down
        if position_down is None:
            self.position_down = length
        else:
            self.position_down = position_down
        self.plasma_dens_top = plasma_dens_top
        self.ramp_type = ramp_type
        self.profile = profile
        
    def track_beam_numerically_RK_parallel(self, beam, steps, non_rel=False, 
                                           n_proc=None):
        """
        Track the beam through the plasma using a 4th order Runge-Kutta method.
        
        Parameters:
        -----------
        beam : ParticleBunch
            Particle bunch to track.

        steps : int
            Number of steps in which output should be given.

        non_rel : bool
            If True, the relativistic assumplion is not used for the equations
            of motion.

        n_proc : int
            Number of processes to run in parallel. If None, this will equal
            the number of physical cores.

        Returns:
        --------
        A list of size 'steps' containing the beam distribution at each step.

        """
        if non_rel:
            raise NotImplementedError()
        else:
            field = PlasmaRampBlowoutField(self.length,
                                             self.plasma_dens_down,
                                             self.plasma_dens_top,
                                             self.position_down,
                                             ramp_type = self.ramp_type,
                                             profile=self.profile)
        # Main beam quantities
        mat = beam.get_6D_matrix()
        # Plasma length in time
        t_final = self.length/ct.c
        t_step = t_final/steps
        dt = self._get_optimized_dt(beam, field)
        iterations = int(t_final/dt)
        # force at least 1 iteration per step
        it_per_step = max(int(iterations/steps), 1)
        iterations = it_per_step*steps
        dt_adjusted = t_final/iterations
        beam_list = list()

        start = time.time()
        if n_proc is None:
            num_proc = cpu_count()
        else:
            num_proc = n_proc
        num_part = mat.shape[1]
        part_per_proc = int(np.ceil(num_part/num_proc))
        process_pool = Pool(num_proc)
        t_s = 0
        matrix_list = list()
        try:
            for p in np.arange(num_proc):
                matrix_list.append(mat[:,p*part_per_proc:(p+1)*part_per_proc])

            for s in np.arange(steps):
                print(s)
                partial_solver = partial(
                    runge_kutta_4, WF=field, dt=dt_adjusted,
                    iterations=it_per_step, t0=s*t_step)
                matrix_list = process_pool.map(partial_solver, matrix_list)
                beam_matrix = np.concatenate(matrix_list, axis=1)
                x, px, y, py, xi, pz = beam_matrix
                new_prop_dist = beam.prop_distance + (s+1)*t_step*ct.c
                beam_list.append(
                    ParticleBunch(beam.q, x, y, xi, px, py, pz,
                                  prop_distance=new_prop_dist)
                    )
        finally:
            process_pool.close()
            process_pool.join()

        end = time.time()
        print("Done ({} seconds)".format(end-start))

        # update beam data
        last_beam = beam_list[-1]
        beam.set_phase_space(last_beam.x, last_beam.y, last_beam.xi,
                             last_beam.px, last_beam.py, last_beam.pz)
        beam.increase_prop_distance(self.length)

        return beam_list
    
    def _get_optimized_dt(self, beam, wakefield):
        gamma = self._gamma(beam.px, beam.py, beam.pz)
        mean_gamma = np.average(gamma, weights=beam.q)
        # calculate maximum focusing on ramp.
        t = np.linspace(0, self.length, 100)/ct.c
        kx = wakefield.calculate_focusing(0, t)
        max_kx = max(kx)
        w_x = np.sqrt(ct.e*ct.c/ct.m_e * max_kx/mean_gamma)
        period_x = 1/w_x
        dt = 0.1*period_x
        return dt

    def _gamma(self, px, py, pz):
        return np.sqrt(1 + np.square(px) + np.square(py) + np.square(pz))
