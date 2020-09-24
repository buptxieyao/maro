# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.


import io
import yaml

from maro.utils import convert_dottable


with io.open("config.yml", "r") as in_file:
    raw_config = yaml.safe_load(in_file)
    config = convert_dottable(raw_config)
