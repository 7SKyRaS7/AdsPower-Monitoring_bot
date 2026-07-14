import sys
import os
import re

with open("adspower_monitor.py", "r", encoding="utf-8") as f:
    code = f.read()

# We need to extract the group resolution into a method: _resolve_groups
# And in the while loop, check if config file modified.

# Let's use multi_replace_file_content or a script to do this.
