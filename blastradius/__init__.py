"""BlastRadius — an infrastructure remediation agent with a blast radius of zero.

The engineering is in the execution shell, not the prompting: every run is
planned (dry-run) before it mutates, approved by a human, executed one
checkpointed step at a time, verified against post-conditions, and rolled
back automatically from recorded inverse actions if verification fails.
"""

__version__ = "0.1.0"
