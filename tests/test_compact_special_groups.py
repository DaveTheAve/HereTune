from heretune import __version__
from heretune.server import _compact_library_groups


def test_special_compact_groups_do_not_consume_folder_letters():
    assert __version__ == "0.13.0"
    source = [
        {"name": "Favorites", "list": ["f"]},
        {"name": "All Movies", "list": ["a"]},
        {"name": "Studio One", "list": ["1"]},
        {"name": "Studio Two", "list": ["2"]},
        {"name": "Studio Three", "list": ["3"]},
    ]
    result = _compact_library_groups(source)
    assert [group["name"] for group in result] == ["♥", "All", "A", "B", "C"]
    assert [group["name"] for group in source] == [
        "Favorites", "All Movies", "Studio One", "Studio Two", "Studio Three"
    ]


def test_all_without_favorites_still_starts_folders_at_a():
    result = _compact_library_groups([
        {"name": "All Movies", "list": []},
        {"name": "Folder X", "list": []},
        {"name": "Folder Y", "list": []},
    ])
    assert [group["name"] for group in result] == ["All", "A", "B"]
