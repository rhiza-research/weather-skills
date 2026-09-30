"""Keep a realistic dataset resident during the benchmark (~0.9 GB: a year of 4 daily variables on a
0.25-degree, Africa-sized grid). Raise N_VARS to probe the memory ceiling."""

import time

import numpy as np

N_VARS = 4
data = np.ones((365, 400, 400, N_VARS), dtype=np.float32)
data += 0  # touch every page so it is really resident
while True:
    time.sleep(3600)
