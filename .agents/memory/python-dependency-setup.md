---
name: Python dependency setup
description: The imported Python environment may need its Replit Python toolchain enabled before package installation succeeds.
---

Install or enable the project’s Python toolchain before installing requirements through the environment package manager. A package install can fail generically when the runtime module is not enabled.

**Why:** The imported project initially had a working interpreter but no scientific/plotting dependencies, and package installation only succeeded after enabling the Python toolchain.

**How to apply:** When an imported Python project’s dependency install fails without useful diagnostics, check available Python modules, enable a compatible one, then retry the declared requirements.