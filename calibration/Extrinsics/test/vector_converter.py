import numpy as np

# milimeters
mmRVCam1 = np.array([0.0744, 0.0650, -1.5338])
mmTVCam1 = np.array([15.9934, -10.1731, 183.8601])
#milimeters
mmRVCam0 = np.array([-0.3589, 0.0426, -1.4990])
mmTVCam0 = np.array([9.3106, 28.0528, 204.8060])

def mm_to_m_converter(arr: np.ndarray) -> np.ndarray:
    for item in arr:
        return arr / 1000
        

def mmVectors():
    RVCam1 = mm_to_m_converter(mmRVCam1)
    RVCam0 = mm_to_m_converter(mmRVCam0)
    TVCam1 = mm_to_m_converter(mmTVCam1)
    TVCam0 = mm_to_m_converter(mmTVCam0)

    print("\n Cam1 Rotation Vector: ", RVCam1)  
    print("\n Cam1 Translation Vector: ", TVCam1)
    print("\n Cam0 Rotation Vector: ", RVCam0)
    print("\n Cam0 Translation Vector: ", TVCam0)


mmVectors()