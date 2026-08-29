# Development tools

Interactive scripts for probing a real door station. They are **not** part of
the running application and are deliberately excluded from the Docker image:
each one takes device credentials and talks to hardware, so shipping them into
the runtime container widened the attack surface for no runtime benefit.

Run them from a checkout, with the app's dependencies installed:

```bash
python -m tools.cli_probe        <device-name-or-id>   # scan candidate endpoints
python -m tools.cli_deep_probe   <device-name-or-id>   # slower, wider scan
python -m tools.cli_diagnose     <device-name-or-id>   # end-to-end health check
```

They read the same `.env` and the same SQLite database as the app, so point
`DATA_DIR` at the volume you want to inspect.
