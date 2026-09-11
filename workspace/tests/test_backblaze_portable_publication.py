"""Missing or foreign publication evidence cannot open the first-catalog gate."""
from copy import deepcopy
from unittest.mock import patch
import pytest

from scripts import backblaze_resources as resources


def fixture_binding():
    stamp = [1, 2, 33152, 501, 20, 1, 123, 1000000, 1000000]
    return {'identity_sha256': 'a' * 64, 'started_utc': '20200101000000',
            'finished_utc': '20200101000100', 'filestats_sha256': 'b' * 64,
            'filestats_stat': stamp,
            'volumes': {mount: {'guid_sha256': 'c' * 64, 'name_sha256': 'd' * 64, 'stat': stamp}
                        for mount in ('/', str(resources.OWC))}}


@pytest.mark.parametrize('value', [None, {}, [], 'old-host-evidence'])
def test_missing_publication_stops_before_reading_vendor_state(value):
    with patch.object(resources, 'FIRST_SCAN_PUBLICATION', value), \
         patch.object(resources, 'bounded_native_bytes', side_effect=AssertionError('native read forbidden')):
        with pytest.raises(resources.BootstrapError, match='binding_missing_or_invalid'):
            resources.require_first_scan_completion({}, 'a' * 64)


@pytest.mark.parametrize('change', ['digest', 'timestamp', 'bool-stat', 'foreign-volume', 'unknown-field'])
def test_invalid_or_different_mount_binding_never_reads_vendor_state(change):
    value = deepcopy(fixture_binding())
    if change == 'digest':
        value['identity_sha256'] = 'example-digest'
    elif change == 'timestamp':
        value['finished_utc'] = 'not-a-native-time'
    elif change == 'bool-stat':
        value['filestats_stat'][0] = True
    elif change == 'foreign-volume':
        value['volumes']['/example/different-volume'] = value['volumes'].pop(str(resources.OWC))
    else:
        value['reviewed'] = True
    with patch.object(resources, 'FIRST_SCAN_PUBLICATION', value), \
         patch.object(resources, 'bounded_native_bytes', side_effect=AssertionError('native read forbidden')):
        with pytest.raises(resources.BootstrapError, match='binding_missing_or_invalid'):
            resources.require_first_scan_completion({}, 'a' * 64)


def test_well_shaped_binding_still_requires_independent_native_evidence():
    with patch.object(resources, 'FIRST_SCAN_PUBLICATION', fixture_binding()), \
         patch.object(resources, 'bounded_native_bytes', side_effect=resources.BootstrapError('native evidence absent')) as read:
        with pytest.raises(resources.BootstrapError, match='native evidence absent'):
            resources.require_first_scan_completion({}, 'a' * 64)
        read.assert_called_once()
