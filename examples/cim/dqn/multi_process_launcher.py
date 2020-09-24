# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""
This script is used to debug distributed algorithm in single host multi-process mode.
"""

import os

from examples.cim.dqn.config import config


learner_path = "components/dist_learner.py &"
actor_path = "components/dist_actor.py &"

for l_num in range(config.distributed.actor.peer["actor"]):
    os.system(f"python " + learner_path)

for a_num in range(config.distributed.learner.peer["actor_worker"]):
    os.system(f"python " + actor_path)
