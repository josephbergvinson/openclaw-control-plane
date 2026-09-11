# Backblaze trial and first-catalog operation

This optional path supports an explicitly requested replacement personal-trial
installation. Enrollment, licensing and Full Disk Access are native prerequisites.
The reference does not enroll a client or establish upload or restore acceptance.
Keep the original hold and request intact, and install the complete internal
package described in [host maintenance](host-maintenance.md) before admission.

## Trial transition with an established catalog

Run the selected Python from the configured workspace with its private
`OPENCLAW_OPERATOR_CONFIG` binding:

```sh
python3 scripts/backblaze_health.py --transition-personal-trial OLD_SHA256 NEW_SHA256 EXPIRES_STATUS HOLD_SHA256
```

The four arguments bind the previously verified original identity, independently
observed replacement identity, exact native `expires_YYYYMMDDhhmmss` status and
hash of the original private hold. Only `trial_15_days_free` is accepted; both
native renewal-failure fields must be `none`. Expiry is UTC, future and no more
than 15 days from transition. Retry cannot extend it.

This command creates private metadata only. It requires a present catalog, fresh
matching native/local license evidence, installed and paused status, manual
scheduling, disabled automatic throttle, one upload thread, actual transmitter
absence, correct volumes and every normal resource gate. The fresh watchdog
receipt must demonstrate protective pause with the exact installed source set.

The transition embeds the original request bytes and hash while leaving the old
hold and request unchanged. Later checks use a separate request named for the new
identity; the initial transition rejects a preexisting replacement request. No old
completion or retry intent is inherited. Before a separately requested ordinary
backup, require a subsequent natural healthy watchdog receipt. Expiry, changed
license/renewal fields, hold, identity or upload configuration revokes the exception
and requires protective pause. A change to paid billing needs separate review.

## Independently bind first-scan publication

An absent catalog still blocks the trial transition. For a separately requested
first-catalog attempt, populate private `backup.first_scan_publication` only after
independent review of the new installation's finalized native publication. It is
optional and defaults to `null`; absence or malformed evidence fails closed.

The object has exactly these fields:

| Field | Required evidence |
| --- | --- |
| `identity_sha256` | Installed replacement HGUID fingerprint |
| `started_utc`, `finished_utc` | Native timestamps in `YYYYMMDDhhmmss` UTC form |
| `filestats_sha256` | Exact finalized `bzfilelists/filestats.xml` bytes |
| `filestats_stat` | Native stat tuple in the order below |
| `volumes` | Exactly `/` and the configured `paths.data_root` mount |

Each volume entry has exactly `guid_sha256`, `name_sha256` and `stat`: hashes of
the selected vendor volume GUID and finalized filelist basename, plus that file's
stat tuple. The nine nonnegative integer stat fields are `dev`, `ino`, `mode`,
`uid`, `gid`, `nlink`, `size`, `mtime_ns`, `ctime_ns`, in that order. Do not reuse
example values or accept a boolean as proof of a completed scan.

Use the native enrollment/scanner observation retained for this same installation
and scan to establish `started_utc`. It must identify an actual start with a known
timezone, converted to UTC. `filestats.xml` supplies the finalized `info.datetime`,
not the beginning of the scan. One supported evidence class is a bounded observation
of the relevant native `bzlogs/bzfilelist/bzfilelistDD.log` under the configured
vendor data root: the event entering main disk analysis with `start_at_dir=0`,
projected as `main_disk_analysis_entered`, its process identity and timestamp as
logged. Bind that event to this enrollment and verify its timezone and full date;
the rotated log basename alone is insufficient. Native installation-time evidence
can corroborate the same scan. Retain the observation and its provenance privately.
Neither a directory timestamp, an old scan receipt,
the newest file nor a chosen time before completion establishes the start. If that
observation is unavailable, leave this optional binding unset and continue through
the supported native enrollment observation; do not manufacture a timestamp.

Once that start is independently verified, this bounded local recipe produces a
new private proposal using the existing helper. Run it with the same configured
Python and `OPENCLAW_OPERATOR_CONFIG` as the health owner. Replace the two arguments
with the verified native UTC start and a new absolute private output path outside
the vendor data tree. It reads only small native XML files and filelist metadata,
validates the complete proposal against native state, then writes an exclusive
owner-only JSON file. It makes no vendor command, configuration or service call.

```sh
python3 - VERIFIED_SCAN_START_UTC /absolute/private/first-scan-publication.json <<'PY'
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from scripts import backblaze_resources as r

started, output = sys.argv[1], Path(sys.argv[2])
stamp = datetime.strptime(started, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
if stamp.strftime('%Y%m%d%H%M%S') != started:
    raise ValueError('exact independently verified UTC scan start required')
if not output.is_absolute() or output.resolve().is_relative_to(r.BZDATA.parent.resolve()):
    raise ValueError('choose a new absolute private output outside vendor data')
install_raw, _ = r.bounded_native_bytes(r.BZDATA.parent / 'bzinstall.xml', 16384)
install = ET.fromstring(install_raw).find('bzuniqueid')
if install is None:
    raise ValueError('native installed identity is unavailable')
identity = r.identity_fingerprint(install.get('hguid'))
config_raw, _ = r.bounded_native_bytes(r.BZDATA / 'bzinfo.xml', 262144)
config = r.bootstrap_config(ET.fromstring(config_raw))
directory = r.BZDATA / 'bzfilelists'
names = r.first_scan_names(directory)
raw, file_stat = r.bounded_native_bytes(directory / 'filestats.xml', 16384)
info = ET.fromstring(raw).find('info')
if info is None:
    raise ValueError('native final scan information is unavailable')
proposal = {'identity_sha256': identity, 'started_utc': started,
            'finished_utc': info.get('datetime'),
            'filestats_sha256': hashlib.sha256(raw).hexdigest(),
            'filestats_stat': file_stat, 'volumes': {}}
for mount, guid in config['volumes'].items():
    matches = [name for name in names if name.startswith(guid + '_') and name.endswith('filelist.dat')]
    if len(matches) != 1:
        raise ValueError('exactly one finalized filelist per selected volume required')
    name = matches[0]
    proposal['volumes'][mount] = {'guid_sha256': hashlib.sha256(guid.encode()).hexdigest(),
                                'name_sha256': hashlib.sha256(name.encode()).hexdigest(),
                                'stat': r.scan_stat((directory / name).lstat())}
r.FIRST_SCAN_PUBLICATION = proposal  # This disposable process only; no config write.
r.require_first_scan_completion(config, identity)
r.atomic_json(output, proposal, exclusive=True)
print('Private native-publication proposal written; independent review remains required.')
PY
```

Review the proposal against the independently observed installation, selected
volumes, scan-start record and finalized publication. Retain that review privately,
then copy the reviewed object into `backup.first_scan_publication` in both the
health owner's and internal watchdog's private operator configuration. The recipe
does not update either configuration. The action-time validator rechecks native
evidence, so a later publication or changed inode requires fresh review rather
than editing a stat value simply to pass a stale binding.

The helper rereads small install/config/final-status files through bounded
regular-file descriptors, checks both reported installation fingerprints and true
final flags, rejects `.future` entries and changed volume/file identities, and
rechecks observations before acceptance. It never opens filelist payloads. Leaf
`O_NOFOLLOW` and before/after checks do not pin every ancestor or atomically exclude
arbitrary concurrent path writers. This evidence establishes native processing
publication only; investigate warnings and coverage independently.

## One first-catalog attempt

```sh
python3 scripts/backblaze_health.py --bootstrap-personal-catalog OLD_SHA256 NEW_SHA256 EXPIRES_STATUS HOLD_SHA256 ADMISSION_PATH ADMISSION_SHA256
```

The private admission file must match the supplied hash and the schema
`openclaw.backblaze_bootstrap_admission.v1`. Its exact fields are `schema`,
`observed_epoch`, `new_identity_sha256`, `full_disk_access` and `enrollment_guard`.
The Full Disk Access map has true current observations for
`com.backblaze.Backblaze` and `com.backblaze.bzbmenu`. The guard map has `pid` and
`start_command_sha256`, hashing trimmed native `ps -p PID -o lstart= -o command=`
output. Verify that exact prior supervisor has drained; retain no arbitrary command
text. The receipt must be at most 120 seconds old at preflight entry and is
rechecked after the bounded native report. It cannot replace first-scan evidence
or justify editing TCC state.

Admission also binds the signed physical vendor CLI, exact configuration, both
volume GUIDs, configured filesystem UUID, source hashes, expiry, original hold and
request bytes. Manual scheduling, disabled automatic throttle, one thread and the
configured external scratch mount remain required. Supported preparation occurs
under verified hold protection. The helper never silently repairs failed settings.

The owner exclusively persists a consumed attempt before its only possible
`bzcli action --backup-now` dispatch. This can upload selected files during
initialization. A crash before launch still consumes the attempt. Replay reconciles
protection or the completed handoff; it cannot dispatch again. The permanent stop
latch dominates mutable phase and heartbeat state even if malformed. Never remove
or rewrite a latch to make a replay look fresh.

The owner heartbeats every five seconds; missing/replaced ownership or a heartbeat
older than 90 seconds revokes the exception. A bounded probe child allows heartbeats
while native controls run. Reports retain a 180-second bound, backup/pause calls
60 seconds and protective configuration 30 seconds. Subprocess waits are outside
the short admin lock. Only owned CLI/probe children may be terminated on timeout;
the transmitter is never killed by this helper. There is no total bootstrap
deadline. Unknown vendor progress, missing counters and an absent initial catalog
are not completion or sufficient evidence of a stall.

The first nonempty regular catalog is a sticky observation. An empty initial file
remains initializing; later missing/invalid catalog state fails strictly. Catalog
creation triggers supported pause and actual drain checks, followed by a fresh
strict native report. Pause acknowledgment alone is insufficient. Only then may
the owner record `catalog_established`. A later natural `bootstrap_paused_catalog`
watchdog receipt can admit the separate metadata transition above; it is not an
ordinary healthy receipt. Fresh selected-file upload, coverage and an independent
restore remain separate gates after all of these checks.
