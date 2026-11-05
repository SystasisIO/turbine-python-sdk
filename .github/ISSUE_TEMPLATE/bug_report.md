---
name: Bug report
about: Create a bug report to help us improve.
title: ""
labels: bug
assignees: ""
---

**Describe the bug**
A clear and concise description of what the bug is.

**SDK version**
Output of `pip show turbine-sdk` (or `python -c "import turbine_sdk; print(turbine_sdk.__version__)"`).

**Python version**
Output of `python --version`.

**Which deployment**
TurbineOS or another compatible self-hosted deployment, including its version.

**Minimal reproduction**
The smallest snippet that reproduces it. Please redact your API key.

```python
from turbine_sdk import Turbine

client = Turbine(base_url="http://127.0.0.1:8189", api_key=None)
# ...
```

**Expected behavior**
A clear and concise description of what you expected to happen.

**Actual behavior**
What happened instead, including the full traceback if there is one.

```
paste traceback here
```

**Nice to have**

- [ ] Terminal output
- [ ] The workflow JSON (or a trimmed version of it)
- [ ] Screenshots

**Additional context**
Add any other context about the problem here.
