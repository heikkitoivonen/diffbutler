# Offline process isolation (M2 foundation)

`reviewers.sandbox.OfflineSandbox` is an internal Linux process primitive. It is
not connected to the scheduler, source reader, CLI, or a real reviewer. Its network
policy denies all network access, including model providers. Real background
reviews remain unavailable.

Each invocation requires Bubblewrap from the system executable search path and a
dedicated, trusted runtime image. Missing Bubblewrap, unsupported platforms, rejected
namespace operations, unsupported flags, and nonzero child exits fail without an
unsandboxed retry. The runner never installs software or changes host security policy.
macOS isolation remains unimplemented.

The fixed profile uses Bubblewrap's user, mount, PID, network, IPC, and UTS namespaces,
drops capabilities, disables further user namespaces, and uses parent-death and
session isolation. See the upstream [Bubblewrap options](https://github.com/containers/bubblewrap/blob/main/bwrap.xml).
The image is mounted read-only, with fresh temporary `/home`, `/tmp`, and `/work`
mounts for every invocation. Host repositories, homes, agent sessions, `/proc`,
devices, sockets, and credentials are not added as mounts. The launcher receives a
minimal environment; the payload receives fixed HOME, TMPDIR, PATH, LC_ALL, and PWD
values. No environment passthrough or caller-supplied sandbox flags are supported.
Inherited file descriptors are closed. All three standard streams are pipes, and
the process has no controlling terminal.

The image is a **trusted input**, not a directory discovered from PR content. It
must contain only the intended executable and its runtime dependencies, plus real
`tmp`, `home`, and `work` directories. It must contain no host secrets, foreground
artifacts, sockets, devices, or nested host mounts. Do not pass a repository, user
home, or host installation tree. The runner checks basic shape and rejects `/` and
symlinked mount destinations; it does not certify arbitrary image contents. The
image and system Bubblewrap installation must remain under trusted administration
and unchanged during execution. Runtime provisioning and manifests are future work.

The API accepts an absolute executable path inside the image and literal argv.
Input bytes go over stdin. Input size and combined stdout/stderr size are bounded;
execution has a timeout. Errors contain fixed diagnostics rather than child output
or host paths. Successful captured bytes remain untrusted, potentially private
data; this primitive does not log or persist them. It kills the owned process group,
drains pipes, and waits for launcher exit on completion, timeout, overflow, and
cancellation, including cancellation while subprocess creation is still completing.
Bubblewrap's PID namespace and parent-death handling are necessary for descendants
that detach from the initial process group.

This is not yet the full SPEC §13 review boundary. There is no model transport,
authentication broker, cross-process source mediation, prompt serialization,
source-only tool policy, session resume/import, persistent process ownership, or
orphan recovery. Temporary files can be written and runtime tools can be executed
inside the sandbox; that does not establish source-only review behavior. Kernel
confinement also does not impose CPU, process-count, or temporary-memory quotas.
Do not connect a real agent or release a durable review claim based on this
primitive alone. Those integration and policy checks remain prerequisites.

## Validation

Fake launchers exercise the fixed argv/environment, literal arguments and stdin,
missing capabilities, safe failure diagnostics, byte budgets, blocked stdin,
timeout, cancellation, and subprocess creation races. These tests validate policy
construction and lifecycle mechanics, **not kernel confinement**.

A separate Linux integration test compiles a synthetic static C executable into
a disposable image. With real Bubblewrap it checks host-file and symlink escape
denial, read-only image mounts, environment and descriptor isolation, absence of
host proc/terminal access, fresh home storage, and denial of host loopback and
abstract Unix socket connections. All data and listeners are synthetic and local.
No forge, credential store, or paid model is used. The test skips explicitly when
Bubblewrap or a compiler is missing; a present but unusable sandbox fails the test.

On the current development host Bubblewrap is absent, so kernel confinement has
**not been validated**. The integration test must pass on a supported Linux host,
and descendant termination needs further adversarial validation, before this is
treated as a verified execution boundary.
