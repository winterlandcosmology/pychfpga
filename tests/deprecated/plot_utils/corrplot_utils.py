"""
corrplot_utils.py script 
Plotting utilities for correlations. The start_corr_capture() fcn from chFPGA_controller must be set for this to work.


#
History:
    2012-11-19 Juan: Created corrplot_utils class and widecorrplot function.
    2012-11-23 Juan: Created narrowcorrplot and waterfallplot functions.
    2012-11-26 Juan: Created matrixplot function.
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from mpl_toolkits.mplot3d import axes3d

class corrplot_utils():
    """
    Creates an object that provides all the correlation plotting utilities        
    """
    def widecorrplot(self,fpgarec,tint=1,tout=3,plottype=0,corrnum=-1,undersampling=1,fs=800.0,showlegend=0,magscale=0,phaunits=0):
        """
        Plots correlations for all frequencies for all correlation indexes in list corrnum (convention for 5 channel correlator: 0=00,1=01,2=02,3=03,4=04,5=11,6=22,...
        14=44). If corrnum=-1 all correlations are plotted. fpgarec is the chFPGA_receiver object (previously created).
        
        - tint is the integration_period (in seconds) used for correlator (default 1sec, check start_corr_capture() in chFPGA_controller).
        - tout is the timeout setting used for reading correlator frames (default 3sec, check read_corr_frames() in chFPGA_receiver).
        - plottype=0,1,2 for plotting of magnitude, phase or magnitude an phase of the correlations respectively.
        - If undersampling is enabled the spectra are flipped andd frequencies are shifted by fs/2. fs is the sampling frequency (MHz).
        - If showlegend is enabled, a legend with all the lines plotted is shown.
        - If magscale=1, then log10(magnitude) is plotted.
        - phaunits=0,1 for phase plotted in rad or deg respectively.

        Example on how to use widecorrplot: Type on python shell (on pychfpga directory)
        >>run -i top_test()         #FPGA initialization. chFPGA_controller object created as c, chFPGA_receiver object created as r
        >>c.set_FFT_bypass(False)   #Enable FFT
        >>c.start_corr_capture()    #Enable correlators with integration period of one second (default)
        >>import corrplot_utils
        >>m=corrplot_utils.corrplot_utils()     # Create corrplot_utils object
        >>m.widecorrplot(r,plottype=2,corrnum=[9,10,12],showlegend=1)   # Plot mag and phase of correlation indexes 9,10,12
        >>c.set_gain(5) # Optional step. Modify the gain (range -1 to 14) of the scalar module to see the correlations properly (check set_gain() in chFPGA_controller)

        To stop the plotting type m.wideani._stop(). It is recommended that the plotting is stopped before creating other plots, closing the figure or
        deleting the object m.
        """
        def update(updateflag): #Update figure
            if plottype==0:
                for i in range(0,self.widecorrmat.shape[0]):
                    if magscale:
                        self.widehlmag[i].set_ydata(np.log10(abs(self.widecorrmat[i])))
                    else:
                        self.widehlmag[i].set_ydata(abs(self.widecorrmat[i]))

            elif plottype==1:
                for i in range(0,self.widecorrmat.shape[0]):
                    if phaunits:
                        self.widehlpha[i].set_ydata(np.rad2deg(np.angle(self.widecorrmat[i])))
                    else:
                        self.widehlpha[i].set_ydata(np.angle(self.widecorrmat[i]))#self.widehlpha[i].set_ydata(np.unwrap(np.angle(self.widecorrmat[i])))

            else:
                for i in range(0,self.widecorrmat.shape[0]):
                    if magscale:
                        self.widehlmag[i].set_ydata(np.log10(abs(self.widecorrmat[i])))
                    else:
                        self.widehlmag[i].set_ydata(abs(self.widecorrmat[i]))

                    if phaunits:
                        self.widehlpha[i].set_ydata(np.rad2deg(np.angle(self.widecorrmat[i])))
                    else:
                        self.widehlpha[i].set_ydata(np.angle(self.widecorrmat[i]))
                        
        def data_gen(): # Get new frame and update data to plot
            while True:
                #fpgarec.flush()
                self.widecorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0)
                self.widecorrmat=self.widecorrmat[corrnum,:]
                if undersampling:
                    self.widecorrmat=np.conj(self.widecorrmat[:,::-1])

                yield 1
            
        fpgarec.flush()
        self.widecorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0)   # Get new object with all correlations
        if corrnum==-1:
            corrnum=range(self.widecorrmat.shape[0])
        
        self.widecorrmat=self.widecorrmat[corrnum,:]    # Select correlations to plot
        self.wideframelength=2*self.widecorrmat.shape[1]    # Get framelength (before fft)
        self.widebwres=(1.0*fs)/self.wideframelength        # Get resolution bandwidth
        self.wideposfreq=np.array(range(self.wideframelength/2))*self.widebwres     # Generate frequency axis
        if undersampling:   # Undersampling at fs/2. Spectrum is flipped and frequency axis adjusted accordingly
            self.wideposfreq=self.wideposfreq+(fs/2)
            self.widecorrmat=np.conj(self.widecorrmat[:,::-1])
    
        self.widehf=plt.figure()
        self.widehf.suptitle('Correlation Spectrum (BWres='+str(self.widebwres)+' MHz, Integr. time='+str(tint)+' s)')
        
        if plottype==0:
            self.widehamag=self.widehf.add_subplot(111)
            self.widehamag.set_xlabel('Frequency (MHz)')
            if magscale:
                self.widehamag.set_ylabel('log10(Correlator Units)')
            else:
                self.widehamag.set_ylabel('Correlator Units')
                
            self.widehamag.grid()
            self.widehlmag=[0]*self.widecorrmat.shape[0]    # list with all the handles for the lines in magnitude plot
            for i in range(self.widecorrmat.shape[0]):
                if magscale:
                    self.widehlmag[i],=self.widehamag.plot(self.wideposfreq,np.log10(abs(self.widecorrmat[i])))
                else:
                    self.widehlmag[i],=self.widehamag.plot(self.wideposfreq,abs(self.widecorrmat[i]))

                self.widehlmag[i].set_label('corrindex '+str(corrnum[i]))

            if showlegend:
                self.widehamag.legend()
                
        elif plottype==1:
            self.widehapha=self.widehf.add_subplot(111)
            self.widehapha.set_xlabel('Frequency (MHz)')
            if phaunits:
                self.widehapha.set_ylabel('Angle (deg)')
            else:
                self.widehapha.set_ylabel('Angle (rad)')

            self.widehapha.grid()
            self.widehlpha=[0]*self.widecorrmat.shape[0]    # list with all the handles for the lines in phase plot
            for i in range(self.widecorrmat.shape[0]):
                if phaunits:
                    self.widehlpha[i],=self.widehapha.plot(self.wideposfreq,np.rad2deg(np.angle(self.widecorrmat[i])))
                else:
                    self.widehlpha[i],=self.widehapha.plot(self.wideposfreq,np.angle(self.widecorrmat[i]))#self.widehlpha[i],=self.widehapha.plot(self.wideposfreq,np.unwrap(np.angle(self.widecorrmat[i])))

                self.widehlpha[i].set_label('corrindex '+str(corrnum[i]))

            if showlegend:
                self.widehapha.legend()
                
        else:
            self.widehamag=self.widehf.add_subplot(121)
            self.widehamag.set_xlabel('Frequency (MHz)')
            if magscale:
                self.widehamag.set_ylabel('log10(Correlator Units)')
            else:
                self.widehamag.set_ylabel('Correlator Units')

            self.widehamag.grid()
            self.widehlmag=[0]*self.widecorrmat.shape[0]
            self.widehapha=self.widehf.add_subplot(122,sharex=self.widehamag)
            self.widehapha.set_xlabel('Frequency (MHz)')
            if phaunits:
                self.widehapha.set_ylabel('Angle (deg)')
            else:
                self.widehapha.set_ylabel('Angle (rad)')

            self.widehapha.grid()
            self.widehlpha=[0]*self.widecorrmat.shape[0]
            
            for i in range(self.widecorrmat.shape[0]):
                if magscale:
                    self.widehlmag[i],=self.widehamag.plot(self.wideposfreq,np.log10(abs(self.widecorrmat[i])))
                else:
                    self.widehlmag[i],=self.widehamag.plot(self.wideposfreq,abs(self.widecorrmat[i]))

                if phaunits:
                    self.widehlpha[i],=self.widehapha.plot(self.wideposfreq,np.rad2deg(np.angle(self.widecorrmat[i])))
                else:
                    self.widehlpha[i],=self.widehapha.plot(self.wideposfreq,np.angle(self.widecorrmat[i]))#self.widehlpha[i],=self.widehapha.plot(self.wideposfreq,np.unwrap(np.angle(self.widecorrmat[i])))

                self.widehlmag[i].set_label('corrindex '+str(corrnum[i]))

            if showlegend:
                self.widehamag.legend()                
                       
        self.wideani = animation.FuncAnimation(self.widehf,update,data_gen,interval=tint*1000)  # create animation object
        plt.show()

    def narrowcorrplot(self,fpgarec,freqs,tint=1,tout=3,plottype=0,corrnum=-1,bufferlength=100,undersampling=1,fs=800.0,showlegend=0,magscale=0,phaunits=0):
        """
        Plots the last bufferlength frames of the correlations for frequencies specified in list freqs (in MHz) for all correlation indexes in list corrnum
        (convention for 5 channel correlator: 0=00,1=01,2=02,3=03,4=04,5=11,6=22,... 14=44). If corrnum=-1 all correlations are plotted for the frequencies in freqs.
        fpgarec is the chFPGA_receiver object (previously created).
        
        - tint is the integration_period (in seconds) used for correlator (default 1sec, check start_corr_capture() in chFPGA_controller).
        - tout is the timeout setting used for reading correlator frames (default 3sec, check read_corr_frames() in chFPGA_receiver).
        - plottype=0,1,2 for plotting of magnitude, phase or magnitude an phase of the correlations respectively.
        - If undersampling is enabled the spectra are flipped and frequencies are shifted by fs/2. fs is the sampling frequency (MHz).
        - If showlegend is enabled, a legend with all the lines plotted is shown.
        - If magscale=1, then log10(magnitude) is plotted.
        - phaunits=0,1 for phase plotted in rad or deg respectively.

        Example on how to use narrowcorrplot: Type on python shell (on pychfpga directory)
        >>run -i top_test()         #FPGA initialization. chFPGA_controller object created as c, chFPGA_receiver object created as r
        >>c.set_FFT_bypass(False)   #Enable FFT
        >>c.start_corr_capture()    #Enable correlators with integration period of one second (default)
        >>import corrplot_utils
        >>m=corrplot_utils.corrplot_utils()     # Create corrplot_utils object
        >>m.narrowcorrplot(r,[600,550.5],plottype=2,corrnum=[9,10,12],showlegend=1)   # Plot mag and phase of frequency bins 600 and 550.5MHz for correlation indexes 9,10,12.
        >>c.set_gain(5) # Optional step. Modify the gain (range -1 to 14) of the scalar module to see the correlations properly (check set_gain() in chFPGA_controller)

        To stop the plotting type m.narrowani._stop(). It is recommended that the plotting is stopped before creating other plots, closing the figure or
        deleting the object m.        
        """

        def update(updateflag): #Update figure
            if plottype==0:
                for i in range(self.narrowcorrmat.shape[0]):
                    for j in range(self.narrowcorrmat.shape[1]):
                        if magscale:
                             self.narrowhlmag[i][j].set_ydata(np.log10(abs(self.narrowcorrbuffer[:,i,j])))
                        else:
                             self.narrowhlmag[i][j].set_ydata(abs(self.narrowcorrbuffer[:,i,j]))

            elif plottype==1:
                for i in range(self.narrowcorrmat.shape[0]):
                    for j in range(self.narrowcorrmat.shape[1]):
                        if phaunits:
                            self.narrowhlpha[i][j].set_ydata(np.rad2deg(np.angle(self.narrowcorrbuffer[:,i,j])))
                        else:
                            self.narrowhlpha[i][j].set_ydata(np.angle(self.narrowcorrbuffer[:,i,j]))

            else:
                for i in range(self.narrowcorrmat.shape[0]):
                    for j in range(self.narrowcorrmat.shape[1]):
                        if magscale:
                             self.narrowhlmag[i][j].set_ydata(np.log10(abs(self.narrowcorrbuffer[:,i,j])))
                        else:
                             self.narrowhlmag[i][j].set_ydata(abs(self.narrowcorrbuffer[:,i,j]))

                        if phaunits:
                            self.narrowhlpha[i][j].set_ydata(np.rad2deg(np.angle(self.narrowcorrbuffer[:,i,j])))
                        else:
                            self.narrowhlpha[i][j].set_ydata(np.angle(self.narrowcorrbuffer[:,i,j]))
                            
        def data_gen(): # Get new frame and update data to plot
            while True:
                self.narrowcorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0)
                self.narrowcorrmat=self.narrowcorrmat[corrnum,:]
                if undersampling:
                    self.narrowcorrmat=np.conj(self.narrowcorrmat[:,::-1])
                    
                self.narrowcorrmat=self.narrowcorrmat[:,self.frindexes]
                if self.narrowcounter<bufferlength:
                    self.narrowcorrbuffer[self.narrowcounter]=self.narrowcorrmat
                    self.narrowcounter=self.narrowcounter+1
                else:
                    self.narrowcorrbuffer=np.roll(self.narrowcorrbuffer,-1,0)
                    self.narrowcorrbuffer[bufferlength-1]=self.narrowcorrmat
                    
                yield 1
            
        fpgarec.flush()
        self.narrowcorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0) # Get new object with all correlations
        if corrnum==-1:
            corrnum=range(self.narrowcorrmat.shape[0])
                   
        self.narrowcorrmat=self.narrowcorrmat[corrnum,:]    # Select correlations to plot
        self.narrowframelength=2*self.narrowcorrmat.shape[1]    # Get framelength (before fft)
        self.narrowbwres=(1.0*fs)/self.narrowframelength    # Get resolution bandwidth
        self.frindexes=np.floor([f/self.narrowbwres for f in freqs])    # Indexes of frequencies to plot      
        if undersampling:   # Undersampling at fs/2. Spectrum is flipped and frequency indexes adjusted accordingly
            self.frindexes=[i-(self.narrowframelength/2) for i in self.frindexes]
            self.narrowcorrmat=np.conj(self.narrowcorrmat[:,::-1])
            
        self.narrowcorrmat=self.narrowcorrmat[:,self.frindexes] # Select frequencies to plot
        self.narrowcorrbuffer=np.zeros((bufferlength,self.narrowcorrmat.shape[0],self.narrowcorrmat.shape[1]))*1j*np.nan    # buffer array with selected correlations
        self.narrowcorrbuffer[0]=self.narrowcorrmat
        self.narrowcounter=int(1)   # counter for the number of filled frames in narrowcorrbuffer

        self.narrowhf=plt.figure()
        self.narrowhf.suptitle('Single Frequency Channel Power (BWres='+str(self.narrowbwres)+' MHz, Integr. time='+str(tint)+' s)')
        
        if plottype==0:
            self.narrowhamag=self.narrowhf.add_subplot(111)
            self.narrowhamag.set_xlabel('Frame number')
            if magscale:
                self.narrowhamag.set_ylabel('log10(Correlator Units)')
            else:
                self.narrowhamag.set_ylabel('Correlator Units')

            self.narrowhamag.set_xlim((1,bufferlength))
            self.narrowhamag.grid()
            self.narrowhlmag=[[0]*self.narrowcorrmat.shape[1] for i in range(self.narrowcorrmat.shape[0])]  # list with all the handles for the lines in magnitude plot
            for i in range(self.narrowcorrmat.shape[0]):
                for j in range(self.narrowcorrmat.shape[1]):
                    if magscale:
                        self.narrowhlmag[i][j],=self.narrowhamag.plot(range(1,bufferlength+1),np.log10(abs(self.narrowcorrbuffer[:,i,j])))                        
                    else:
                        self.narrowhlmag[i][j],=self.narrowhamag.plot(range(1,bufferlength+1),abs(self.narrowcorrbuffer[:,i,j]))

                    self.narrowhlmag[i][j].set_label('corr '+str(corrnum[i])+', f='+str(freqs[j])+' MHz')

            if showlegend:
                self.narrowhamag.legend()
                
        elif plottype==1:
            self.narrowhapha=self.narrowhf.add_subplot(111)
            self.narrowhapha.set_xlabel('Frame number')
            if phaunits:
                self.narrowhapha.set_ylabel('Angle (deg)')
            else:
                self.narrowhapha.set_ylabel('Angle (rad)')

            self.narrowhapha.set_xlim((1,bufferlength))
            self.narrowhapha.grid()
            self.narrowhlpha=[[0]*self.narrowcorrmat.shape[1] for i in range(self.narrowcorrmat.shape[0])]  # list with all the handles for the lines in phase plot
            for i in range(self.narrowcorrmat.shape[0]):
                for j in range(self.narrowcorrmat.shape[1]):
                    if phaunits:
                        self.narrowhlpha[i][j],=self.narrowhapha.plot(range(1,bufferlength+1),np.rad2deg(np.angle(self.narrowcorrbuffer[:,i,j])))
                    else:
                        self.narrowhlpha[i][j],=self.narrowhapha.plot(range(1,bufferlength+1),np.angle(self.narrowcorrbuffer[:,i,j]))

                    self.narrowhlpha[i][j].set_label('corr '+str(corrnum[i])+', f='+str(freqs[j])+' MHz')

            if showlegend:
                self.narrowhapha.legend()
                
        else:
            self.narrowhamag=self.narrowhf.add_subplot(121)
            self.narrowhamag.set_xlabel('Frame number')
            if magscale:
                self.narrowhamag.set_ylabel('log10(Correlator Units)')
            else:
                self.narrowhamag.set_ylabel('Correlator Units')

            self.narrowhamag.set_xlim((1,bufferlength))
            self.narrowhamag.grid()
            self.narrowhlmag=[[0]*self.narrowcorrmat.shape[1] for i in range(self.narrowcorrmat.shape[0])]
            self.narrowhapha=self.narrowhf.add_subplot(122,sharex=self.narrowhamag)
            self.narrowhapha.set_xlabel('Frame number')
            self.narrowhapha.set_xlabel('Frame number')
            if phaunits:
                self.narrowhapha.set_ylabel('Angle (deg)')
            else:
                self.narrowhapha.set_ylabel('Angle (rad)')

            self.narrowhapha.set_xlim((1,bufferlength))            
            self.narrowhapha.grid()
            self.narrowhlpha=[[0]*self.narrowcorrmat.shape[1] for i in range(self.narrowcorrmat.shape[0])]
            for i in range(self.narrowcorrmat.shape[0]):
                for j in range(self.narrowcorrmat.shape[1]):
                    if magscale:
                        self.narrowhlmag[i][j],=self.narrowhamag.plot(range(1,bufferlength+1),np.log10(abs(self.narrowcorrbuffer[:,i,j])))                        
                    else:
                        self.narrowhlmag[i][j],=self.narrowhamag.plot(range(1,bufferlength+1),abs(self.narrowcorrbuffer[:,i,j]))

                    if phaunits:
                        self.narrowhlpha[i][j],=self.narrowhapha.plot(range(1,bufferlength+1),np.rad2deg(np.angle(self.narrowcorrbuffer[:,i,j])))
                    else:
                        self.narrowhlpha[i][j],=self.narrowhapha.plot(range(1,bufferlength+1),np.angle(self.narrowcorrbuffer[:,i,j]))

                    self.narrowhlmag[i][j].set_label('corr '+str(corrnum[i])+', f='+str(freqs[j])+' MHz')

            if showlegend:
                self.narrowhamag.legend()          
                       
        self.narrowani = animation.FuncAnimation(self.narrowhf,update,data_gen,interval=tint*1000) # create animation object
        plt.show()


    def waterfallplot(self,fpgarec,corrnum,tint=1,tout=3,plottype=0,bufferlength=100,undersampling=1,fs=800.0,magscale=0,phaunits=0):
        """
        3d Plot of the last bufferlength frames of correlation corrnum (convention for 5 channel correlator: 0=00,1=01,2=02,3=03,4=04,5=11,6=22,... 14=44).
        fpgarec is the chFPGA_receiver object (previously created). corrnum must be an integer.
        
        - tint is the integration_period (in seconds) used for correlator (default 1sec, check start_corr_capture() in chFPGA_controller).
        - tout is the timeout setting used for reading correlator frames (default 3sec, check read_corr_frames() in chFPGA_receiver).
        - plottype=0,1,2 for plotting of magnitude, phase or magnitude an phase of the correlation respectively.
        - If undersampling is enabled the spectra are flipped and frequencies are shifted by fs/2. fs is the sampling frequency (MHz).
        - If magscale=1, then log10(magnitude) is plotted.
        - phaunits=0,1 for phase plotted in rad or deg respectively.

        Example on how to use waterfallplot: Type on python shell (on pychfpga directory)
        >>run -i top_test()         #FPGA initialization. chFPGA_controller object created as c, chFPGA_receiver object created as r
        >>c.set_FFT_bypass(False)   #Enable FFT
        >>c.start_corr_capture()    #Enable correlators with integration period of one second (default)
        >>import corrplot_utils
        >>m=corrplot_utils.corrplot_utils()     # Create corrplot_utils object
        >>m.waterfallplot(r,corrnum=10,plottype=0)   # 3d mag Plot of last 100 frames for correlation index 10.
        >>c.set_gain(5) # Optional step. Modify the gain (range -1 to 14) of the scalar module to see the correlations properly (check set_gain() in chFPGA_controller)

        To stop the plotting type m.wfallani._stop(). It is recommended that the plotting is stopped before creating other plots, closing the figure or
        deleting the object m.        
        """

        def update(updateflag): #Update figure
            if plottype==0:
                for i in range(bufferlength):
                    self.wfallhlmag[i].set_xdata(self.wfallposfreq)
                    self.wfallhlmag[i].set_ydata([i+1]*np.ones((self.wfallframelength/2,1)))
                    if magscale:
                        self.wfallhlmag[i].set_3d_properties(np.log10(abs(self.wfallcorrbuffer[i])))
                    else:
                        self.wfallhlmag[i].set_3d_properties(abs(self.wfallcorrbuffer[i]))

            elif plottype==1:
                for i in range(bufferlength):
                    self.wfallhlpha[i].set_xdata(self.wfallposfreq)
                    self.wfallhlpha[i].set_ydata([i+1]*np.ones((self.wfallframelength/2,1)))
                    if phaunits:
                        self.wfallhlpha[i].set_3d_properties(np.rad2deg(np.angle(self.wfallcorrbuffer[i])))
                    else:
                        self.wfallhlpha[i].set_3d_properties(np.angle(self.wfallcorrbuffer[i]))
                        
            else:
                for i in range(bufferlength):
                    self.wfallhlmag[i].set_xdata(self.wfallposfreq)
                    self.wfallhlmag[i].set_ydata([i+1]*np.ones((self.wfallframelength/2,1)))
                    if magscale:
                        self.wfallhlmag[i].set_3d_properties(np.log10(abs(self.wfallcorrbuffer[i])))
                    else:
                        self.wfallhlmag[i].set_3d_properties(abs(self.wfallcorrbuffer[i]))

                    self.wfallhlpha[i].set_xdata(self.wfallposfreq)
                    self.wfallhlpha[i].set_ydata([i+1]*np.ones((self.wfallframelength/2,1)))
                    if phaunits:
                        self.wfallhlpha[i].set_3d_properties(np.rad2deg(np.angle(self.wfallcorrbuffer[i])))
                    else:
                        self.wfallhlpha[i].set_3d_properties(np.angle(self.wfallcorrbuffer[i]))
                        
        def data_gen(): # Get new frame and update data to plot
            while True:
                #fpgarec.flush()
                self.wfallcorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0)
                self.wfallcorrmat=self.wfallcorrmat[[corrnum,],:]
                if undersampling:
                    self.wfallcorrmat=np.conj(self.wfallcorrmat[:,::-1])

                if self.wfallcounter<bufferlength:
                    self.wfallcorrbuffer[self.wfallcounter]=self.wfallcorrmat
                    self.wfallcounter=self.wfallcounter+1
                else:
                    self.wfallcorrbuffer=np.roll(self.wfallcorrbuffer,-1,0)
                    self.wfallcorrbuffer[bufferlength-1]=self.wfallcorrmat
                                        
                yield 1
            
        fpgarec.flush()
        self.wfallcorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0)  # Get new object with all correlations
        self.wfallcorrmat=self.wfallcorrmat[[corrnum,],:]   # Select correlation to plot
        self.wfallframelength=2*self.wfallcorrmat.shape[1]  # Framelength (before fft)
        self.wfallbwres=(1.0*fs)/self.wfallframelength      # Bandwidth resolution
        self.wfallposfreq=np.array(range(self.wfallframelength/2))*self.wfallbwres  # Generate frequency axis
        if undersampling:   # Undersampling at fs/2. Spectrum is flipped and frequency axis adjusted accordingly
            self.wfallposfreq=self.wfallposfreq+(fs/2)
            self.wfallcorrmat=np.conj(self.wfallcorrmat[:,::-1])
            
        self.wfallcorrbuffer=np.zeros((bufferlength,self.wfallframelength/2))*1j*np.nan # buffer 2d array to keep bufferlength frames in plot
        self.wfallcorrbuffer[0]=self.wfallcorrmat
        self.wfallcounter=int(1) # counter for the number of frames already in wfallcorrbuffer

        self.wfallhf=plt.figure()
        self.wfallhf.suptitle('Correlation '+str(corrnum)+' (BWres='+str(self.wfallbwres)+' MHz, Integr. time='+str(tint)+' s)')
        
        if plottype==0:
            self.wfallhamag=self.wfallhf.add_subplot(111,projection='3d')
            self.wfallhamag.set_xlabel('Frequency (MHz)')
            self.wfallhamag.set_ylabel('Frame number')
            if magscale:
                self.wfallhamag.set_zlabel('log10(Correlator Units)')
            else:
                self.wfallhamag.set_zlabel('Correlator Units')
                
            self.wfallhamag.set_ylim((1,bufferlength))
            self.wfallhlmag=[0]*bufferlength # list with all the handles for the lines in magnitude plot
            for i in range(bufferlength):
                if magscale:
                    self.wfallhlmag[i],=self.wfallhamag.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),np.log10(abs(self.wfallcorrbuffer[i])),'b')
                else:
                    self.wfallhlmag[i],=self.wfallhamag.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),abs(self.wfallcorrbuffer[i]),'b')                    
                
        elif plottype==1:
            self.wfallhapha=self.wfallhf.add_subplot(111, projection='3d')
            self.wfallhapha.set_xlabel('Frequency (MHz)')
            self.wfallhapha.set_ylabel('Frame number')
            if phaunits:
                self.wfallhapha.set_zlabel('Angle (deg)')
            else:
                self.wfallhapha.set_zlabel('Angle (rad)')
                
            self.wfallhapha.set_ylim((1,bufferlength))
            self.wfallhlpha=[0]*bufferlength    # list with all the handles for the lines in phase plot
            for i in range(bufferlength):
                if phaunits:
                    self.wfallhlpha[i],=self.wfallhapha.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),np.rad2deg(np.angle(self.wfallcorrbuffer[i])),'b')
                else:
                    self.wfallhlpha[i],=self.wfallhapha.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),np.angle(self.wfallcorrbuffer[i]),'b')                    
                
        else:
            self.wfallhamag=self.wfallhf.add_subplot(121,projection='3d')
            self.wfallhamag.set_xlabel('Frequency (MHz)')
            self.wfallhamag.set_ylabel('Frame number')
            if magscale:
                self.wfallhamag.set_zlabel('log10(Correlator Units)')
            else:
                self.wfallhamag.set_zlabel('Correlator Units')

            self.wfallhamag.set_ylim((1,bufferlength))
            self.wfallhlmag=[0]*bufferlength
            for i in range(bufferlength):
                if magscale:
                    self.wfallhlmag[i],=self.wfallhamag.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),np.log10(abs(self.wfallcorrbuffer[i])),'b')
                else:
                    self.wfallhlmag[i],=self.wfallhamag.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),abs(self.wfallcorrbuffer[i]),'b')
                    
            self.wfallhapha=self.wfallhf.add_subplot(122,projection='3d')
            self.wfallhapha.set_xlabel('Frequency (MHz)')
            self.wfallhapha.set_ylabel('Frame number')
            if phaunits:
                self.wfallhapha.set_zlabel('Angle (deg)')
            else:
                self.wfallhapha.set_zlabel('Angle (rad)')

            self.wfallhapha.set_ylim((1,bufferlength))
            self.wfallhlpha=[0]*bufferlength
            for i in range(bufferlength):
                if phaunits:
                    self.wfallhlpha[i],=self.wfallhapha.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),np.rad2deg(np.angle(self.wfallcorrbuffer[i])),'b')
                else:
                    self.wfallhlpha[i],=self.wfallhapha.plot(self.wfallposfreq,[i+1]*np.ones((self.wfallframelength/2,1)),np.angle(self.wfallcorrbuffer[i]),'b')                    
                                
        self.wfallani = animation.FuncAnimation(self.wfallhf,update,data_gen,interval=tint*1000)    #create animation object
        plt.show()

        
    def matrixplot(self,fpgarec,corrnum,tint=1,tout=3,plottype=0,bufferlength=100,undersampling=1,fs=800.0,magscale=0,phaunits=0):
        """
        Plots a matrix (displayed as an image) with the last bufferlength frames of correlation corrnum (convention for 5 channel correlator: 0=00,1=01,2=02,
        3=03,4=04,5=11,6=22,... 14=44). fpgarec is the chFPGA_receiver object (previously created). corrnum must be an integer.
        
        - tint is the integration_period (in seconds) used for correlator (default 1sec, check start_corr_capture() in chFPGA_controller).
        - tout is the timeout setting used for reading correlator frames (default 3sec, check read_corr_frames() in chFPGA_receiver).
        - plottype=0,1,2 for plotting of magnitude, phase or magnitude an phase of the correlation respectively.
        - If undersampling is enabled the spectra are flipped and frequencies are shifted by fs/2. fs is the sampling frequency (MHz).
        - If magscale=1, then log10(magnitude) is plotted.
        - phaunits=0,1 for phase plotted in rad or deg respectively.

        Example on how to use matrixplot: Type on python shell (on pychfpga directory)
        >>run -i top_test()         #FPGA initialization. chFPGA_controller object created as c, chFPGA_receiver object created as r
        >>c.set_FFT_bypass(False)   #Enable FFT
        >>c.start_corr_capture()    #Enable correlators with integration period of one second (default)
        >>import corrplot_utils
        >>m=corrplot_utils.corrplot_utils()     # Create corrplot_utils object
        >>m.matrixplot(r,corrnum=10,plottype=2)   # Plots (as matrix image) mag and phase of last 100 frames for correlation index 10.
        >>c.set_gain(5) # Optional step. Modify the gain (range -1 to 14) of the scalar module to see the correlations properly (check set_gain() in chFPGA_controller)

        To stop the plotting type m.matani._stop(). It is recommended that the plotting is stopped before creating other plots, closing the figure or
        deleting the object m.            
        """

        def update(updateflag): #Update figure
            if plottype==0:
                if magscale:
                    self.mathlmag.set_array(np.log10(abs(self.matcorrbuffer)))
                    self.mathlmag.set_clim([np.nanmin(np.log10(abs(self.matcorrbuffer))),np.nanmax(np.log10(abs(self.matcorrbuffer)))])
                    
                else:
                    self.mathlmag.set_array(abs(self.matcorrbuffer))
                    self.mathlmag.set_clim([np.nanmin(abs(self.matcorrbuffer)),np.nanmax(abs(self.matcorrbuffer))])                    

            elif plottype==1:
                if phaunits:
                    self.mathlpha.set_array(np.rad2deg(np.angle(self.matcorrbuffer)))
                    self.mathlpha.set_clim([np.nanmin(np.rad2deg(np.angle(self.matcorrbuffer))),np.nanmax(np.rad2deg(np.angle(self.matcorrbuffer)))])
                    
                else:
                    self.mathlpha.set_array(np.angle(self.matcorrbuffer))
                    self.mathlpha.set_clim([np.nanmin(np.angle(self.matcorrbuffer)),np.nanmax(np.angle(self.matcorrbuffer))])                    
                        
            else:
                if magscale:
                    self.mathlmag.set_array(np.log10(abs(self.matcorrbuffer)))
                    self.mathlmag.set_clim([np.nanmin(np.log10(abs(self.matcorrbuffer))),np.nanmax(np.log10(abs(self.matcorrbuffer)))])
                    
                else:
                    self.mathlmag.set_array(abs(self.matcorrbuffer))
                    self.mathlmag.set_clim([np.nanmin(abs(self.matcorrbuffer)),np.nanmax(abs(self.matcorrbuffer))])

                if phaunits:
                    self.mathlpha.set_array(np.rad2deg(np.angle(self.matcorrbuffer)))
                    self.mathlpha.set_clim([np.nanmin(np.rad2deg(np.angle(self.matcorrbuffer))),np.nanmax(np.rad2deg(np.angle(self.matcorrbuffer)))])
                    
                else:
                    self.mathlpha.set_array(np.angle(self.matcorrbuffer))
                    self.mathlpha.set_clim([np.nanmin(np.angle(self.matcorrbuffer)),np.nanmax(np.angle(self.matcorrbuffer))])              
                        
        def data_gen(): # Get new frame and update data to plot
            while True:
                #fpgarec.flush()
                self.matcorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0)
                self.matcorrmat=self.matcorrmat[[corrnum,],:]
                if undersampling:
                    self.matcorrmat=np.conj(self.matcorrmat[:,::-1])

                if self.matcounter<bufferlength:
                    self.matcorrbuffer[self.matcounter]=self.matcorrmat
                    self.matcounter=self.matcounter+1
                else:
                    self.matcorrbuffer=np.roll(self.matcorrbuffer,-1,0)
                    self.matcorrbuffer[bufferlength-1]=self.matcorrmat
                                        
                yield 1
            
        fpgarec.flush()
        self.matcorrmat=fpgarec.read_corr_frames(timeout=tout,verbose=0)    # Get new object with all correlations
        self.matcorrmat=self.matcorrmat[[corrnum,],:]   # Select correlation to plot
        self.matframelength=2*self.matcorrmat.shape[1]  # length of frames (before fft)
        self.matbwres=(1.0*fs)/self.matframelength  # resolution bandwidth
        self.matposfreq=np.array(range(self.matframelength/2))*self.matbwres    # frequency axis
        if undersampling:   # Undersampling at fs/2. Spectrum is flipped and frequency axis adjusted accordingly
            self.matposfreq=self.matposfreq+(fs/2)
            self.matcorrmat=np.conj(self.matcorrmat[:,::-1])
            
        self.matcorrbuffer=np.zeros((bufferlength,self.matframelength/2))*1j*np.nan # buffer 2d array to keep bufferlength frames in plot
        self.matcorrbuffer[0]=self.matcorrmat
        self.matcounter=int(1) # counter for the number of frames already in matcorrbuffer

        self.mathf=plt.figure()
        self.mathf.suptitle('Correlation '+str(corrnum)+' (BWres='+str(self.matbwres)+' MHz, Integr. time='+str(tint)+' s)')
        
        if plottype==0:
            self.mathamag=self.mathf.add_subplot(111)
            self.mathamag.set_xlabel('Frequency (MHz)')
            self.mathamag.set_ylabel('Frame number')
            if magscale:
                self.mathlmag=self.mathamag.imshow(np.log10(abs(self.matcorrbuffer)),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbmag=plt.colorbar(self.mathlmag,ax=self.mathamag)
                self.mathcbmag.set_label('log10(Correlator Units)')
                
            else:
                self.mathlmag=self.mathamag.imshow(abs(self.matcorrbuffer),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbmag=plt.colorbar(self.mathlmag,ax=self.mathamag)
                self.mathcbmag.set_label('Correlator Units')
                
        elif plottype==1:
            self.mathapha=self.mathf.add_subplot(111)
            self.mathapha.set_xlabel('Frequency (MHz)')
            self.mathapha.set_ylabel('Frame number')
            if phaunits:
                self.mathlpha=self.mathapha.imshow(np.rad2deg(np.angle(self.matcorrbuffer)),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbpha=plt.colorbar(self.mathlpha,ax=self.mathapha)
                self.mathcbpha.set_label('angle (deg)')
                
            else:
                self.mathlpha=self.mathapha.imshow(np.angle(self.matcorrbuffer),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbpha=plt.colorbar(self.mathlpha,ax=self.mathapha)
                self.mathcbpha.set_label('angle (rad)')
                
        else:
            self.mathamag=self.mathf.add_subplot(121)
            self.mathamag.set_xlabel('Frequency (MHz)')
            self.mathamag.set_ylabel('Frame number')
            if magscale:
                self.mathlmag=self.mathamag.imshow(np.log10(abs(self.matcorrbuffer)),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbmag=plt.colorbar(self.mathlmag,ax=self.mathamag)
                self.mathcbmag.set_label('log10(Correlator Units)')
                
            else:
                self.mathlmag=self.mathamag.imshow(abs(self.matcorrbuffer),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbmag=plt.colorbar(self.mathlmag,ax=self.mathamag)
                self.mathcbmag.set_label('Correlator Units')
                
            self.mathapha=self.mathf.add_subplot(122)
            self.mathapha.set_xlabel('Frequency (MHz)')
            self.mathapha.set_ylabel('Frame number')
            if phaunits:
                self.mathlpha=self.mathapha.imshow(np.rad2deg(np.angle(self.matcorrbuffer)),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbpha=plt.colorbar(self.mathlpha,ax=self.mathapha)
                self.mathcbpha.set_label('angle (deg)')
                
            else:
                self.mathlpha=self.mathapha.imshow(np.angle(self.matcorrbuffer),interpolation='None',aspect='auto',extent=[self.matposfreq[0],self.matposfreq[-1],bufferlength,1])
                self.mathcbpha=plt.colorbar(self.mathlpha,ax=self.mathapha)
                self.mathcbpha.set_label('angle (rad)')                  
                                
        self.matani = animation.FuncAnimation(self.mathf,update,data_gen,interval=tint*1000) # Create animation object
        plt.show()
