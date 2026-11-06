/* Human review: only the task prompt is editable; Gold and Judge stay untouched. */
let groundingPromptState = null;
function groundingPromptEdit(editing) {
  if (!groundingPromptState) return;
  document.getElementById('gr-prompt-display').hidden = editing;
  document.getElementById('gr-prompt-edit').hidden = editing;
  document.getElementById('gr-prompt-form').hidden = !editing;
  const input = document.getElementById('gr-prompt-input');
  input.value = groundingPromptState.prompt;
  taskDirty = editing;
  if (editing) input.focus();
}
async function groundingPromptSave() {
  const state = groundingPromptState, input = document.getElementById('gr-prompt-input');
  if (!state || !input || state.folder !== currentFolder) return;
  const status = document.getElementById('gr-prompt-status');
  const buttons = document.querySelectorAll('#gr-prompt-form button');
  if (!input.value.trim()) { status.textContent = GeoUI.t('题面不能为空'); return; }
  buttons.forEach(b => b.disabled = true);
  status.textContent = GeoUI.t('保存中…');
  try {
    const response = await fetch(`/api/task/save?folder=${encodeURIComponent(state.folder)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task_type:state.taskType, fields:{user_prompt:input.value}})
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw Error(result.error || `HTTP ${response.status}`);
    if (currentFolder !== state.folder) return;
    taskDirty = false;
    await refreshManifest();
    await selectTask(state.folder);
  } catch (error) {
    if (currentFolder === state.folder) status.textContent = error.message;
  } finally { buttons.forEach(b => b.disabled = false); }
}
function renderPlacePromptEditor(task) {
  groundingPromptState = {folder:task.folder_ref || task.folder || currentFolder || '', prompt:task.user_prompt || '', taskType:task.task_type};
  return `<div class="section"><div class="gr-prompt-heading"><div class="section-title">📋 User Prompt</div><button id="gr-prompt-edit" class="nav-btn" onclick="groundingPromptEdit(true)" data-ui-text>编辑题面</button></div><div id="gr-prompt-display" class="prompt-box">${esc(task.user_prompt || '(empty)')}</div>
    <div id="gr-prompt-form" hidden><textarea id="gr-prompt-input" class="prompt-box" aria-label="User Prompt"></textarea><div class="gr-prompt-actions"><small data-ui-text>保存后需重新审核；不会修改图片、答案或 Judge。</small><button class="nav-btn" onclick="groundingPromptEdit(false)" data-ui-text>取消</button><button class="btn-save" onclick="groundingPromptSave()" data-ui-text>保存题面</button></div><div id="gr-prompt-status" role="status"></div></div></div>`;
}
function renderGroundingReview(data) {
  const task = data.task || {}, summary = task.summary || {};
  const source = data.curated_source || {}, ex = source.example || {};
  const assets = summary.input_assets || {};
  const folder = task.folder_ref || task.folder || currentFolder || '';
  const label = text => `<span data-ui-text>${esc(text)}</span>`;
  const value = item => typeof item === 'string' ? item : JSON.stringify(item, null, 2);
  const raw = (title, item) => `<details class="gr-details"><summary>${label(title)}</summary><pre>${esc(value(item ?? {}))}</pre></details>`;
  const image = (path, title) => {
    if (!path) return `<p class="readonly-note">${label('未记录图片')}</p>`;
    const url = `/tasks/api/asset?path=${encodeURIComponent(path)}&folder=${encodeURIComponent(folder)}`;
    return `<figure class="gr-image"><a href="${attr(url)}" target="_blank" rel="noopener"><img src="${attr(url)}" alt="${attr(title)}" loading="lazy"></a><figcaption>${label(title)} · ${label('点击图片放大')}</figcaption></figure>`;
  };
  let html = renderPlacePromptEditor(task);
  // Keep the established map/image cards, sizing and layout above the evidence.
  html += renderGroundingImage(task, data.grounding_gold || {}, data.world_slice || {});
  let evidence = `<section class="gr-evidence"><h3>${label('线索与依据')}</h3><p class="readonly-note">${label('以下为构建记录，不代表全部内容已提供给模型。')}</p><div class="gr-clue-grid">`;
  for (const [key, title] of [['visual_clues', '视觉线索'], ['text_clues', '文字线索'], ['negative_clues', '排除性线索']]) {
    const clues = Array.isArray(ex[key]) ? ex[key] : [];
    evidence += `<article class="gr-clue-card"><h4>${label(title)}<span class="gr-clue-count">${clues.length}</span></h4>`;
    evidence += clues.length ? `<ul class="gr-clues">${clues.map((clue, index) => {
      const refs = ex.prompt_evidence_map?.[key];
      const ref = Array.isArray(refs) ? refs[index] : null;
      return `<li><div>${esc(value(clue))}</div>${ref ? `<details class="gr-clue-ref"><summary>${label('记录的依据引用')}</summary><pre>${esc(value(ref))}</pre></details>` : `<small>${label('记录的依据引用')} · ${label('未记录')}</small>`}</li>`;
    }).join('')}</ul>` : `<p class="readonly-note">${label('未记录')}</p>`;
    evidence += '</article>';
  }
  evidence += '</div></section>';
  let records = '<div class="gr-records">';
  if (assets.prompt_reference_image_path || assets.resolved_prompt_reference_image_path) records += `<details class="gr-details"><summary>${label('构建参考图（不输入模型）')}</summary>${image(assets.prompt_reference_image_path || assets.resolved_prompt_reference_image_path, '构建参考图')}</details>`;
  records += raw('构建时的候选范围', ex.candidate_scope);
  records += raw('构建检查记录', ex.quality_gate);
  records += raw('来源与原始记录', {source_path: source.source_path, prompt_evidence_map: ex.prompt_evidence_map,
    evidence: ex.evidence_ledger || ex.source_evidence, source_prompt: ex.user_prompt,
    source_review_translation: ex.user_prompt_translation || ex.review_translation,
    input_assets: assets, gold: data.grounding_gold, legacy_events: data.env_events});
  records += '</div>';
  const profile = ex.quality_profile || ex.difficulty_profile || summary.difficulty_profile || {};
  const quality = profile.rubric_version?.startsWith('place_grounding.quality')
    ? renderGroundingQualityPanel({...ex, ambiguity_landscape: ex.ambiguity_landscape || data.grounding_gold?.ambiguity_landscape || {}}, profile, evidence, records)
    : `<div class="difficulty-panel quality-panel"><div class="difficulty-panel-title">${label('质量审核面板 · Quality Profile')}</div>${renderDifficultyBreakdown(profile)}${evidence}${records}</div>`;
  return html + `<section class="section gr-review">${quality}</section>` + renderReviewControls(task.task_type, data.review_status);
}
