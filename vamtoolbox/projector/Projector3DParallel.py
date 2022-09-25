try:
    import astra
    astra_available = True
except:
    ImportError("Astra toolbox is either not installed or installed incorrectly.")
    astra_available = False
import numpy as np
from skimage.transform._warps import warp
from functools import partial
import multiprocessing as mlp
from tqdm import tqdm
import itertools

import vamtoolbox

class Projector3DParallelAstra():
    def __init__(self, target_geo, proj_geo):
        self.target_geo = target_geo
        self.proj_geo = proj_geo
        self.nT = target_geo.nX
        
        self.angles_rad = np.deg2rad(proj_geo.angles)

        
        self.proj_geom = astra.create_proj_geom('parallel', 1.0, self.nT, self.angles_rad)
        self.vol_geom = astra.create_vol_geom(target_geo.nY, target_geo.nX)
        self.proj_id = astra.create_projector('line', self.proj_geom, self.vol_geom)


    def forward(self,x):
        """Forward projector operation (b = Ax)"""
        x = vamtoolbox.util.data.clipToCircle(x)
        b = np.zeros((self.nT,self.proj_geo.n_angles,self.target_geo.nZ))
        if self.proj_geo.absorption_coeff is not None:
            x = self.proj_geo.absorption_mask*x
        
        for z_i in range(self.target_geo.nZ):
            b_id, tmp_b = astra.create_sino(x[:,:,z_i], self.proj_id)
            b[:,:,z_i] = np.transpose(tmp_b)
            astra.data3d.delete(b_id)

        return b


    def backward(self,b):
        """Backward projector operation (x = A^Tb)"""
        
        x = np.zeros((self.target_geo.nX,self.target_geo.nY,self.target_geo.nZ))
        for z_i in range(self.target_geo.nZ):
            if self.proj_geo.zero_dose_sino is not None:
                b[self.proj_geo.zero_dose_sino] = 0.0
            x_id, tmp_x = astra.creators.create_backprojection(np.transpose(b[:,:,z_i]), self.proj_id)   
            x[:,:,z_i] = tmp_x
            astra.data3d.delete(x_id)

        if self.proj_geo.absorption_coeff is not None:
            x = self.proj_geo.absorption_mask*x
        

        return vamtoolbox.util.data.clipToCircle(x)













class Projector3DParallelPython():
    def __init__(self, target_geo, proj_geo):
        self.target_geo = target_geo
        self.proj_geo = proj_geo
        self.angles = proj_geo.angles

        # setup fixed coordinate grid for backprojection and dimensions of projections  
        self.radius = target_geo.nY//2
        self.y, self.x = np.mgrid[:target_geo.nY, :target_geo.nY] - self.radius
        self.center = target_geo.nY//2
        self.proj_t = np.arange(target_geo.nY) - target_geo.nY//2

        # Occlusion sinogram is computed in init because it will remain the same for a 
        # given instance of the class; all forward/backprojections with the instance 
        # assume that the occlusion does not change
        if self.proj_geo.attenuation_field is not None:
            #TODO make independent of infinite value of insert, e.g. attenuated forward and backward projection
            insert = np.where(self.proj_geo.attenuation_field>0,1,0).astype(int)
            self.occ_sinogram = self.generateOccSinogram(insert)

    def generateOccSinogram(self,occ_array):
        """
        Create sinogram containing minimum values of 's' within the occlusion map

        Returns
        ---------------
        occ_sinogram : nd_array
        Npixels x Nangles x Nslices

        """

        occ_sinogram = np.zeros((self.target_geo.nY,self.angles.shape[0],self.target_geo.nZ))

        for z_i in range(self.target_geo.nZ):
            for i, angle in enumerate(np.deg2rad(self.angles)):
                cos_a, sin_a = np.cos(angle), np.sin(angle)

                R = np.array([[cos_a, sin_a, -self.center * (cos_a + sin_a - 1)],
                            [-sin_a, cos_a, -self.center * (cos_a - sin_a - 1)],
                            [0, 0, 1]])

                rotated_occlusion = warp(occ_array[:,:,z_i], R, clip=True)
                s_occ = np.where(rotated_occlusion>0,self.y,np.NaN)
                
                # disp.view_plot(s_occ,'S')
                
                occ_sinogram[:, i, z_i] = np.nanmin(s_occ,axis=0)

        return occ_sinogram


    def _forward_R(self, angle):
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        R = np.array([[cos_a, sin_a, -self.center * (cos_a + sin_a - 1)],
                      [-sin_a, cos_a, -self.center * (cos_a - sin_a - 1)],
                      [0, 0, 1]])
        return R


    def _forward_mask(self, rotated, angle_i, z_i):
        if self.proj_geo.attenuation_field is not None:  # mask shadow
            curr_occ = self.occ_sinogram[:, angle_i, z_i]  # IO
            if np.count_nonzero(curr_occ) - np.sum(np.isnan(curr_occ)) != 0:
                occ_shadow = self.y > curr_occ[np.newaxis, :]

                rotated = np.multiply(rotated, np.logical_not(occ_shadow))
        return rotated


    def _forward(self, target, z_i, angle_i, angle, return_tuple: bool = False):
        rotated = warp(
            target[:, :, z_i],
            self._forward_R(angle),
            clip=True
        )  # rotate image
        rotated = self._forward_mask(rotated, angle_i, z_i)  # mask image
        integral = rotated.sum(0)  # return integral

        if return_tuple:
            return integral, z_i, angle_i
        else:
            return integral


    def forward(self, target, n_processes: int = 1) -> np.array:
        """
        Computes forward Radon transform of the target space object accounting for
        reduced projection contribution due to occlusion shadowing

        Inputs
        ---------------
        target : nd_array
        Npixels x Npixels x Npixels array that contains the target space object

        Returns
        ---------------
        projection : nd_array
        Npixels x Nangles x Nslices array of forward Radon transform with occlusion shadowing

        """
        projection = np.zeros((self.target_geo.nY,self.angles.shape[0],self.target_geo.nZ))

        z_range = range(self.target_geo.nZ)
        angle_range = np.deg2rad(self.angles)
        iterator = itertools.product(z_range, enumerate(angle_range))
        pbar = tqdm(iterator, total=len(z_range) * len(angle_range), desc=f'{self.__class__.__name__} forward')

        if n_processes == 1:
            for z_i, (angle_i, angle) in pbar:
                projection[:, angle_i, z_i] = self._forward(target, z_i, angle_i, angle)
        else:
            pool = mlp.pool(processes=n_processes if n_processes else mlp.cpu_count())

            results = []
            for z_i, (angle_i, angle) in iterator:
                result = pool.apply_async(
                    func=self._forward,
                    args=(target, z_i, angle_i, angle, True),
                    callback=pbar.update(),
                )
                results.append(result)

            pool.join()
            pool.close()

            for result in results:
                integral, z_i, angle_i = result.get()
                projection[:, angle_i, z_i] = integral

        return projection


    def _backward(self, curr_proj, z_i, angle_i, angle, return_tuple: bool = False):
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        t = self.x * cos_a - self.y * sin_a
        s = self.x * sin_a + self.y * cos_a

        curr_backproj = np.interp(x=t, xp=self.proj_t, fp=curr_proj, left=0, right=0)

        if self.proj_geo.attenuation_field is not None:
            curr_occ = self.getOccShadow(angle_i, z_i, angle, t, s)
            if np.count_nonzero(curr_occ) - np.sum(np.isnan(curr_occ)) != 0:
                curr_backproj += np.multiply(curr_backproj, np.logical_not(curr_occ))

            # plt.imshow(np.multiply(curr_backproj,np.logical_not(curr_occ)),cmap='CMRmap')
            # plt.show()

        if return_tuple:
            return curr_backproj, z_i, angle_i
        else:
            return curr_backproj


    def backward(self,projection, n_processes: int = 1):
        """
        Computes inverse Radon transform of projection accounting for reduced dose
        deposition due to occlusion shadowing

        Inputs
        ---------------
        projection : nd_array
        Npixels x Nangles x Nslices array that contains the projection space sinogram of the target

        Returns
        ---------------
        reconstruction : nd_array
        Npixels x Npixels x Npixels array of inverse Radon transform with occlusion shadowing

        """

        z_range = range(self.target_geo.nZ)
        angle_range = np.deg2rad(self.angles)
        iterator = itertools.product(z_range, enumerate(angle_range))
        pbar = tqdm(iterator, total=len(z_range) * len(angle_range), desc=f'{self.__class__.__name__} backward')

        reconstruction = np.zeros_like(self.target_geo.array)

        if n_processes == 1:
            for z_i, (angle_i, angle) in pbar:
                curr_proj = projection[:, angle_i, z_i]
                reconstruction[:,:,z_i] += self._backward(curr_proj, z_i, angle_i, angle)
        else:
            with mlp.pool(processes=n_processes if n_processes else mlp.cpu_count()) as pool:
                results  = []
                for z_i, (angle_i, angle) in iterator:
                    curr_proj = projection[:, angle_i, z_i]
                    result = pool.apply_async(
                        func=self._backward,
                        args=(curr_proj, z_i, angle_i, angle, True),
                        callback=pbar.update(),
                    )
                    results.append(result)

                for result in results:
                    curr_backproj, z_i, angle_i = result.get()  # TODO timeout
                    reconstruction[:, :, z_i] += curr_backproj

        return vamtoolbox.util.data.clipToCircle(reconstruction)

    def getOccShadow(self,i,j,angle,t,s):
        curr_occ = self.occ_sinogram[:,:,j]
        interpolant_ = np.interp(t, xp=self.proj_t, fp=curr_occ[:,i], left=np.NaN, right=np.NaN)
        return s > np.floor(interpolant_)

    # def calcVisibility(self):
    #     tmp = np.zeros((self.target_obj.nY,self.target_obj.nX,self.angles.shape[0]))
    #     vis = np.zeros(self.target_obj.target.shape)
    #     projection = np.ones((self.target_obj.nY,self.angles.shape[0]))

    #     for i, (curr_proj, angle) in enumerate(zip(projection.T, np.deg2rad(self.angles))):
            
    #         cos_a, sin_a = np.cos(angle), np.sin(angle)

    #         t = self.x * cos_a - self.y * sin_a
    #         s = self.x * sin_a + self.y * cos_a

    #         interpolant = partial(np.interp, xp=self.proj_t, fp=curr_proj*self.angles[i], left=0, right=0)
    #         curr_backproj = interpolant(t)

    #         curr_occ = self.getOccShadow(i,angle,t,s)


    #         tmp[..., i] = np.multiply(curr_backproj,np.logical_not(curr_occ))

    #     for k in range(self.target_obj.nY):
    #         for j in range(self.target_obj.nX):
    #             q = np.unique(tmp[k,j,:]%(self.angles.shape[0]//2))
                
    #             vis[k,j] = q.shape[0]

    #     vis = np.multiply(vis,self.target_obj.target)
    #     vis = vis/(self.angles.shape[0]//2)
    #     vis = np.where(vis >= 1, 1, vis)

    #     return vamtoolbox.util.data.clipToCircle(vis)