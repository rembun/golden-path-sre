#!/usr/bin/env python3
"""Turn a PrometheusRule CRD into a plain Prometheus rule file, so `promtool`
can check it and run unit tests against it (promtool doesn't read CRDs)."""
import sys

import yaml

crd = yaml.safe_load(open(sys.argv[1]))
yaml.safe_dump(crd["spec"], open(sys.argv[2], "w"), sort_keys=False, width=10000)
