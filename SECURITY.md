# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through [GitHub Security Advisories](https://github.com/zmustafa/AzureVMScheduler/security/advisories/new).
Include what you found, how to reproduce it, and the impact you think it has. You will get an
acknowledgement within a few days.

## What this application can do

Azure VM Scheduler starts and stops virtual machines. Treat an instance of it as a privileged
system: whoever controls it can power down production.

Because of that, the defaults are deliberately restrictive:

- **Both action gates ship off.** `ENABLE_REAL_AZURE_STARTS` and `ENABLE_REAL_AZURE_STOPS` are
  `false` in the deployment template. Until an operator turns one on, every wave runs against a
  deterministic mock adapter and touches nothing in Azure.
- **The gates are separate on purpose.** A wrong start costs money; a wrong stop causes an outage.
  Enabling starts never enables stops.
- **Each gate is ANDed with a per-tenant permission.** A global gate alone is not sufficient — the
  Azure connection must also permit that action, and a connection marked read-only can never do
  either.
- **`never_stop`** on a virtual machine or any ancestor group removes it from every stop wave and
  from on-demand stops.
- **On-demand stops require an exact confirmation count** before they run.

## Credentials and secrets

- Azure client secrets and certificates are encrypted at rest with Fernet (AES-128-CBC +
  HMAC-SHA256) and are never returned to the browser.
- The encryption key comes from `FERNET_KEY` when set; the Azure template supplies it as a
  Container App secret. Otherwise a key is generated once into the data directory.
- Prefer **managed identity**: deploy with the template's system-assigned identity, add an Azure
  tenant using the `default_chain` auth method, and grant that identity only the rights it needs.
  Nothing is then stored at all.
- Passwords are hashed with Argon2. Sessions are database-backed and revocable, cookies are
  `HttpOnly` and `Secure` in production, and every mutating request requires a CSRF token.

## Least privilege for the managed identity

Grant reader on the scope you want to manage, plus a custom role limited to the actions the
scheduler actually performs:

```
Microsoft.Compute/virtualMachines/start/action
Microsoft.Compute/virtualMachines/deallocate/action
Microsoft.Compute/virtualMachines/powerOff/action
```

Virtual Machine Contributor also works but grants far more than this application needs.

## Supported versions

The latest published image tag receives security fixes.
