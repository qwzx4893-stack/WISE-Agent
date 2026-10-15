"""Atomic file publication with bounded Windows sharing-lock recovery."""
import os
import time


def replace_file(source,destination):
    # Keep the old complete file until replace succeeds. Never delete it,
    # weaken permissions, or fall back to an in-place partial write.
    for attempt in range(6):
        try:return os.replace(source,destination)
        except PermissionError as error:
            if os.name!='nt' or getattr(error,'winerror',None) not in {None,5,32,33} or attempt==5:raise
            time.sleep(.05*(2**attempt))
