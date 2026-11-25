(function () {
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
    });
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function setNotice(text) { $('notice').textContent = text || ''; }

  function renderBatch(batch) {
    var lines = [];
    lines.push('<li>批次 <code>' + esc(batch.batch_id) + '</code>' +
      (batch.idempotent_replay ? '（幂等回放）' : '') + '</li>');
    (batch.published || []).forEach(function (item) {
      lines.push('<li><span class="verdict" data-v="PASS">发布</span> ' +
        esc(item.station_id) + ' · ' + esc(item.month) + '</li>');
    });
    (batch.held || []).forEach(function (item) {
      lines.push('<li><span class="verdict" data-v="REVIEW">待核</span> ' +
        esc(item.station_id) + ' · ' + esc(item.month) + '</li>');
    });
    (batch.problems || []).forEach(function (item) {
      lines.push('<li><code>' + esc(item.code) + '</code> ' + esc(item.sample_id || '') + '</li>');
    });
    $('corr-list').innerHTML = lines.join('') || '<li class="empty">无变更</li>';
  }

  async function submitCorrection() {
    var sampleId = $('corr-sample').value.trim();
    var correctedAt = $('corr-at').value.trim();
    var key = $('corr-key').value.trim();
    if (!sampleId || !correctedAt || !key) { setNotice('请填写样本编号、更正后时间和幂等键'); return; }
    try {
      setNotice('');
      var batch = await request('/api/corrections', {
        method: 'POST',
        body: JSON.stringify({
          idempotency_key: key,
          profile: $('profile').value,
          corrections: [{sample_id: sampleId, corrected_sample_at: correctedAt}]
        })
      });
      renderBatch(batch);
      setNotice('更正批次已处理：发布 ' + (batch.published || []).length +
        ' 个月份，待核 ' + (batch.held || []).length + ' 个月份');
    } catch (error) { setNotice(error.message); }
  }

  async function showCurrent() {
    try {
      setNotice('');
      var data = await request('/api/corrections/current?profile=' + encodeURIComponent($('profile').value));
      $('corr-list').innerHTML = data.results.length ? data.results.map(function (item) {
        var state = item.review_state === 'PENDING_REVIEW' ?
          '<span class="verdict" data-v="REVIEW">待核</span>' :
          '<span class="verdict" data-v="PASS">现行</span>';
        return '<li>' + state + ' ' + esc(item.station_id) + ' · ' + esc(item.month) +
          ' · v' + esc(item.version) + ' · 均值 ' + esc(Number(item.mean).toFixed(4)) +
          ' · ' + esc(item.count) + ' 条</li>';
      }).join('') : '<li class="empty">暂无现行版本</li>';
    } catch (error) { setNotice(error.message); }
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('corr-submit').addEventListener('click', submitCorrection);
    $('corr-current').addEventListener('click', showCurrent);
  });
})();
