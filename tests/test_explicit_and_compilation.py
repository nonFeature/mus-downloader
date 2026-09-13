import pytest
from metadata import is_compilation_album, resolve_query_metadata
from sources.youtube_matcher import score_candidate, find_best_youtube_match

def test_is_compilation_album():
    # True compilations
    assert is_compilation_album('Rare RnB & New Jack 71') is True
    assert is_compilation_album('Pop - Dance Mania Super Raccolta') is True
    assert is_compilation_album('Now That\'s What I Call Music! 100') is True
    assert is_compilation_album('The Very Best of Daft Punk') is True
    assert is_compilation_album('Top 50 Club Hits 2020') is True
    assert is_compilation_album('Soundtrack: Fast & Furious') is True
    assert is_compilation_album('Random Access Memories', album_artist='Various Artists') is True
    assert is_compilation_album('Cool Album', album_artist='VA') is True

    # Studio albums (not compilations)
    assert is_compilation_album('Random Access Memories', album_artist='Daft Punk') is False
    assert is_compilation_album('Random Access Memories (10th Anniversary Edition)', album_artist='Daft Punk') is False
    assert is_compilation_album('DAMN.', album_artist='Kendrick Lamar') is False
    assert is_compilation_album('DAMN. COLLECTORS EDITION.', album_artist='Kendrick Lamar') is False
    assert is_compilation_album('Discovery', album_artist='Daft Punk') is False
    assert is_compilation_album('Homework', album_artist='Daft Punk') is False

def test_score_candidate_explicit_preference():
    track = {
        'title': 'HUMBLE.',
        'artist': 'Kendrick Lamar',
        'duration': 177,
        'explicit': True
    }
    
    cand_explicit = {
        'title': 'HUMBLE.',
        'author': 'Kendrick Lamar - Topic',
        'duration_seconds': 177,
        'isExplicit': True,
        'source': 'music'
    }
    
    cand_clean = {
        'title': 'HUMBLE.',
        'author': 'Kendrick Lamar - Topic',
        'duration_seconds': 177,
        'isExplicit': False,
        'source': 'music'
    }
    
    cand_radio_edit = {
        'title': 'HUMBLE. (Clean Radio Edit)',
        'author': 'Kendrick Lamar - Topic',
        'duration_seconds': 177,
        'isExplicit': False,
        'source': 'music'
    }
    
    score_exp = score_candidate(track, cand_explicit)
    score_clean = score_candidate(track, cand_clean)
    score_radio = score_candidate(track, cand_radio_edit)
    
    assert score_exp > score_clean
    assert score_clean > score_radio
    assert score_exp >= 0.90
    assert score_radio < 0.60

def test_score_candidate_clean_requested():
    track = {
        'title': 'HUMBLE. (Clean)',
        'artist': 'Kendrick Lamar',
        'duration': 177,
    }
    
    cand_explicit = {
        'title': 'HUMBLE.',
        'author': 'Kendrick Lamar - Topic',
        'duration_seconds': 177,
        'isExplicit': True,
        'source': 'music'
    }
    
    cand_clean = {
        'title': 'HUMBLE. (Clean Version)',
        'author': 'Kendrick Lamar - Topic',
        'duration_seconds': 177,
        'isExplicit': False,
        'source': 'music'
    }
    
    score_exp = score_candidate(track, cand_explicit)
    score_clean = score_candidate(track, cand_clean)
    
    assert score_clean > score_exp

def test_resolve_query_metadata_canonical():
    meta = resolve_query_metadata('Daft Punk - Get Lucky')
    assert meta is not None
    assert 'Rare RnB' not in (meta.get('album') or '')
    assert meta.get('year') in ('2013', '2014', '2023')
    assert 'Random Access Memories' in (meta.get('album') or '')
    assert 'Daft Punk' in (meta.get('artist') or '')
    assert meta.get('track_number') == 8
