import pytest
from task_module.task_types.place_grounding.batch_generation import llm_writer as writer
from task_module.task_types.place_grounding.batch_generation.pipeline import _primary_image_path


def assets(tmp_path):
    result = []
    for i, role in enumerate(['evaluation', 'prompt_reference', 'candidate', 'candidate']):
        path = tmp_path / f'{i}.jpg'
        path.write_bytes(b'image')
        result.append({'path': str(path), 'role': role, 'viewpoint': {'heading': i*90}})
    return {'image_assets': result}


def test_four_images_with_roles_and_export_alignment(tmp_path):
    evidence = assets(tmp_path)
    images = writer._writer_images(evidence)
    assert len(images) == 4
    assert str(images[0]['path']) == _primary_image_path(evidence)
    content = writer._user_content_with_images('payload', images)
    assert len([p for p in content if p['type']=='image_url']) == 4
    assert 'Final task image' in content[1]['text']
    assert 'image_assets[0]' in content[1]['text']
    assert 'Viewing direction: 270' in content[7]['text']
    assert all('Authoring reference image' in content[i]['text'] for i in (3,5,7))


def test_failed_missing_and_duplicate_images_excluded(tmp_path):
    evidence = assets(tmp_path)
    evidence['image_assets'][1]['quality'] = 'fetch_failed'
    evidence['image_assets'][2]['path'] = str(tmp_path/'missing.jpg')
    evidence['image_assets'].append(dict(evidence['image_assets'][0]))
    images = writer._writer_images(evidence)
    assert [i['index'] for i in images] == [0,3]


def test_final_image_is_first_even_if_metadata_is_reordered(tmp_path):
    evidence = assets(tmp_path)
    evidence['image_assets'].reverse()
    images = writer._writer_images(evidence)
    assert images[0]['index'] == 3
    assert str(images[0]['path']) == _primary_image_path(evidence)


def test_missing_final_file_rejected(tmp_path):
    evidence = assets(tmp_path)
    evidence['image_assets'][0]['path'] = str(tmp_path/'missing.jpg')
    with pytest.raises(FileNotFoundError):
        writer._writer_images(evidence)
