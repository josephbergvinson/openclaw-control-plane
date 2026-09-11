"""Discovery omits descriptions; a selected card retains its whole description."""
import json
from unittest.mock import Mock, patch

from scripts import trello_inspect


def test_board_discovery_uses_metadata_fields_and_no_action_history(capsys):
    client = Mock()
    client.get.side_effect = [{'id': 'board', 'name': 'Fixture'}, [{'id': 'card', 'name': 'Meeting'}]]
    with patch.object(trello_inspect, 'build_client', return_value=client), \
         patch('sys.argv', ['trello_inspect', 'board', 'board', '--cards']):
        assert trello_inspect.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['cards'][0]['id'] == 'card'
    assert client.get.call_args_list[1].args == ('/boards/board/cards', {'fields': 'id,name,idList,closed,url,idLabels'})
    assert client.get.call_count == 2


def test_selected_card_preserves_description_and_urls_without_implicit_actions(capsys):
    description = ('Full relevant detail. ' * 300) + 'https://example.com/meeting'
    client = Mock()
    client.get.return_value = {'id': 'card', 'desc': description, 'url': 'https://example.com/card'}
    with patch.object(trello_inspect, 'build_client', return_value=client), \
         patch('sys.argv', ['trello_inspect', 'card', 'card']):
        assert trello_inspect.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['card']['desc'] == description
    assert 'actions' not in result
    client.get.assert_called_once_with('/cards/card', {'fields': 'name,desc,id,idBoard,idList,url,labels,closed,shortLink'})


def test_selected_actions_are_explicit_and_bounded(capsys):
    client = Mock()
    client.get.side_effect = [{'id': 'card'}, []]
    with patch.object(trello_inspect, 'build_client', return_value=client), \
         patch('sys.argv', ['trello_inspect', 'card', 'card', '--actions-limit', '7']):
        assert trello_inspect.main() == 0
    assert json.loads(capsys.readouterr().out)['actions'] == []
    assert client.get.call_args_list[1].args == ('/cards/card/actions', {'limit': 7, 'filter': 'commentCard,updateCard,createCard'})
