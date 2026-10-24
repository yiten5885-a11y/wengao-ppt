/* 稳稿工作台渲染层：所有区域从 state 渲染；无数据时显示空态，不编造。 */
(function () {
  "use strict";
  var WB = window.WB;

  var STEP_LABELS = {
    read: "读取材料", research: "联网检索", compare: "横向对比", trend: "纵向趋势",
    images: "配图", layout: "版式", transition: "转场", check: "核对",
  };
  var STEP_ORDER = ["read", "research", "compare", "trend", "images", "layout", "transition", "check"];

  // ------------------------------------------------------------ 左栏
  WB.renderJobList = function () {
    var st = WB.state;
    var list = document.getElementById("job-list");
    var empty = document.getElementById("job-list-empty");
    var jobs = st.jobs.slice().sort(function (a, b) {
      return (b.created_at || "").localeCompare(a.created_at || "");
    });
    var q = (document.getElementById("search-input").value || "").trim();
    if (q) {
      jobs = jobs.filter(function (j) {
        return ((j.job_id || "") + " " + (j.input_path || "") + " " + (j.theme_name || ""))
          .toLowerCase().indexOf(q.toLowerCase()) >= 0;
      });
    }
    if (st.filter === "imported") jobs = jobs.filter(function (j) { return j.mode === "beautify"; });
    if (st.filter === "research") jobs = jobs.filter(function (j) { return j.has_research; });

    document.getElementById("count-all").textContent = st.jobs.length;
    document.getElementById("count-imported").textContent =
      st.jobs.filter(function (j) { return j.mode === "beautify"; }).length;
    document.getElementById("count-research").textContent =
      st.jobs.filter(function (j) { return j.has_research; }).length;

    if (!jobs.length) {
      list.innerHTML = "";
      empty.hidden = false;
      empty.textContent = q ? "没有匹配的演示。" : "还没有演示。点「新建演示」开始，或把材料拖进中间输入区。";
      return;
    }
    empty.hidden = true;
    var html = "";
    var today = new Date().toDateString();
    var lastGroup = null;
    jobs.forEach(function (j) {
      var d = j.created_at ? new Date(j.created_at) : null;
      var group = d ? (d.toDateString() === today ? "今天" : d.toDateString()) : "更早";
      if (group !== lastGroup) {
        html += '<div class="job-group-label">' + WB.esc(group) + "</div>";
        lastGroup = group;
      }
      var badge = "";
      if (j.validation_verdict === "READY" || j.export_path) badge = '<span class="badge ok">已导出</span>';
      else if (j.validation_verdict === "FAILED") badge = '<span class="badge warn">未通过</span>';
      else if (j.validation_verdict === "NEEDS_REVIEW") badge = '<span class="badge warn">待复核</span>';
      html += '<button type="button" role="option" aria-selected="' + (j.job_id === st.currentJobId) +
        '" class="job-item' + (j.job_id === st.currentJobId ? " is-current" : "") +
        '" data-job="' + WB.esc(j.job_id) + '">' +
        '<div class="t">' + WB.esc(j.input_path ? j.input_path.split("/").pop() : j.job_id) + badge + "</div>" +
        '<div class="s">' + WB.esc(j.mode === "beautify" ? "导入的 PPT" : "材料生成") +
        (j.pages ? " · " + j.pages + " 页" : "") + "</div></button>";
    });
    list.innerHTML = html;
    Array.prototype.forEach.call(list.querySelectorAll(".job-item"), function (el) {
      el.addEventListener("click", function () { WB.selectJob(el.getAttribute("data-job")); });
    });
  };

  WB.renderLocalrun = function () {
    var d = WB.state.localrun;
    var box = document.getElementById("localrun-rows");
    if (!d || !d.rows) return;
    var html = "";
    d.rows.forEach(function (r) {
      html += '<span class="dot ' + (r.ok ? "ok" : "off") + '" aria-hidden="true"></span>' +
        "<span>" + WB.esc(r.label) + "</span>" +
        '<span class="d" title="' + WB.esc(r.detail) + '">' + WB.esc(r.detail) + "</span>";
    });
    box.innerHTML = html;
  };

  // ------------------------------------------------------------ 中栏头部与步骤
  WB.renderJobHead = function () {
    var st = WB.state;
    var title = document.getElementById("job-title");
    var sub = document.getElementById("job-sub");
    if (!st.currentJobId) { title.textContent = "新建演示"; sub.textContent = "把文章、文案或 PPT 交给我"; return; }
    var job = st.job || {};
    var j = (st.jobs || []).filter(function (x) { return x.job_id === st.currentJobId; })[0] || {};
    title.textContent = (j.input_path ? j.input_path.split("/").pop() : st.currentJobId);
    var parts = [];
    if (j.pages) parts.push(j.pages + " 页");
    if (st.terminal) {
      parts.push({ READY: "已通过核对", READY_WITH_EXCEPTIONS: "已通过核对（有降级项）",
                   FAILED: "核对未通过", NEEDS_REVIEW: "待人工复核",
                   CANCELLED: "已取消", DELIVERED: "已交付",
                   DELIVERED_WITH_DEGRADES: "已交付（有降级项）",
                   BLOCKED: "核对未通过" }[st.terminal] || st.terminal);
    } else if (st.running) {
      parts.push("进行中");
    }
    if (j.theme_name) parts.push(j.theme_name);
    sub.textContent = parts.join(" · ") || "进行中";
  };

  WB.renderTimeline = function () {
    var st = WB.state;
    var chat = document.getElementById("chat");
    var empty = document.getElementById("empty-state");
    if (!st.currentJobId) { chat.hidden = true; empty.hidden = false; return; }
    empty.hidden = true;
    chat.hidden = false;

    // 用户消息（任务输入）
    var userHtml = "";
    var j = (st.jobs || []).filter(function (x) { return x.job_id === st.currentJobId; })[0] || {};
    userHtml += '<div class="msg-user">';
    if (j.input_path) {
      userHtml += '<div class="file-chip">' +
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 1.5h5l3 3v10H4z"/><path d="M9 1.5v3h3"/></svg>' +
        WB.esc(j.input_path.split("/").pop()) + "</div>";
    }
    userHtml += '<div class="bubble">' + WB.esc(j.mode === "beautify" ? "把这份 PPT 做得更专业" : "把这份材料做成汇报，补上行业对比和趋势") + "</div></div>";

    // 步骤卡：每步取该 stage 的最后一条事件
    var latest = {};
    st.events.forEach(function (ev) { latest[ev.stage] = ev; });
    var doneCount = STEP_ORDER.filter(function (s) {
      return latest[s] && latest[s].status !== "running";
    }).length;
    var started = st.events.length > 0;
    var stepsHtml = "";
    if (!started) {
      stepsHtml = '<div class="steps-card"><div class="steps-head"><span class="t">步骤</span></div>' +
        '<div class="steps-grid"><span></span><span></span><span class="pending">等待任务开始…</span></div></div>';
    } else {
      stepsHtml = '<div class="steps-card"><div class="steps-head">' +
        '<span class="t">已完成 ' + doneCount + ' / ' + STEP_ORDER.length + ' 步</span>' +
        '<span class="s">' + (st.running ? "进行中" : "已结束") + "</span></div>" +
        '<div class="steps-grid">';
      STEP_ORDER.forEach(function (s) {
        var ev = latest[s];
        var icon, detail;
        if (!ev) {
          icon = iconCircle("#C9CED8", "");
          detail = '<span class="pending">未开始</span>';
        } else if (ev.status === "running") {
          icon = iconCircle("#2A4BD7", "");
          detail = WB.esc(ev.summary || "进行中");
        } else if (ev.status === "failed") {
          icon = iconWarn();
          detail = WB.esc(ev.summary || "未通过");
        } else if (ev.status === "warn" || ev.status === "skipped") {
          icon = iconWarn();
          detail = WB.esc(ev.summary || "已降级");
        } else {
          icon = iconCheck();
          detail = WB.esc(ev.summary || "完成");
        }
        stepsHtml += icon + '<span class="step-name">' + STEP_LABELS[s] + "</span>" +
          '<span class="step-detail">' + detail + "</span>";
      });
      stepsHtml += "</div></div>";
    }

    // 结论文案 + 建议操作
    var tailHtml = WB.renderChatExtras();
    chat.innerHTML = userHtml + '<div class="msg-agent">' + stepsHtml + tailHtml + "</div>";
    var scroller = document.getElementById("mid-scroll");
    scroller.scrollTop = scroller.scrollHeight;
  };

  function iconCheck() {
    return '<svg class="step-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="#15803D"/>' +
      '<path d="M5 8.2l2 2 4-4.4" fill="none" stroke="#FFFFFF" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }
  function iconWarn() {
    return '<svg class="step-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="#B45309"/>' +
      '<path d="M8 4.4v4.3M8 11v.1" fill="none" stroke="#FFFFFF" stroke-width="1.8" stroke-linecap="round"/></svg>';
  }
  function iconCircle(color, _) {
    return '<svg class="step-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="' + color + '"/>' +
      '<circle cx="8" cy="8" r="2.4" fill="#FFFFFF"/></svg>';
  }

  // 冲突待确认卡 / 降级说明 / 失败原因 / 建议操作
  WB.renderChatExtras = function () {
    var st = WB.state;
    var html = "";
    // 冲突卡
    var conflicts = (st.decisions && st.decisions.conflicts) || [];
    var open = conflicts.filter(function (c) { return c.decision === "open"; });
    open.forEach(function (c) {
      html += '<div class="confirm-card">' +
        '<div class="confirm-title">' +
        '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="#B45309"/><path d="M8 4.4v4.3M8 11v.1" fill="none" stroke="#FFFFFF" stroke-width="1.8" stroke-linecap="round"/></svg>' +
        "需要你确认：同一个指标有两个数</div>" +
        '<div class="confirm-cols">' +
        '<div class="confirm-col"><div class="k">你的材料</div><div class="v">' + WB.esc(c.user_value || "—") + '</div><div class="src">来自你上传的材料</div></div>' +
        '<div class="confirm-col"><div class="k">公开来源</div><div class="v">' + WB.esc(c.web_value || "—") + '</div><div class="src">' + WB.esc(c.web_publisher || "公开来源") + "</div></div>" +
        "</div>" +
        '<div class="confirm-actions">' +
        '<button type="button" class="btn btn-dark" data-decision="use_web" data-cid="' + WB.esc(c.conflict_id) + '">采用公开来源</button>' +
        '<button type="button" class="btn btn-ghost" data-decision="use_mine" data-cid="' + WB.esc(c.conflict_id) + '">保留我的数据</button>' +
        '<span class="confirm-note">两个数不会互相覆盖，选定后会写进页面脚注。</span>' +
        "</div></div>";
    });
    // 降级说明
    var t4 = st.images && st.images.counts ? st.images.counts.t4 : 0;
    if (t4 > 0) {
      html += '<div class="degrade-note">' +
        '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#B45309" stroke-width="1.6" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M8 4.5v4M8 10.8v.2"/></svg>' +
        "<span>有 " + t4 + " 页未配图：检索和本机生成都没有合格图片，已按无图版式补位，不影响出稿。</span></div>";
    }
    // 建议操作
    if (st.currentJobId && !st.running) {
      var chips = ["把封面换一张图", "检查全部页面的来源脚注", "导出为 PPTX"];
      html += '<div class="suggest-chips">' + chips.map(function (c) {
        return '<button type="button" class="chip" data-suggest="' + WB.esc(c) + '">' + WB.esc(c) + "</button>";
      }).join("") + "</div>";
    }
    return html;
  };

  // ------------------------------------------------------------ 右栏：来源
  WB.renderSources = function () {
    var st = WB.state;
    var intro = document.getElementById("sources-intro");
    var list = document.getElementById("source-list");
    var d = st.research;
    document.getElementById("tab-count-sources").textContent =
      d && d.available ? (d.counts.sources || 0) : 0;
    if (!d || !d.available) {
      intro.textContent = d && d.reason ? d.reason : "这份演示没有联网研究证据。";
      list.innerHTML = "";
      return;
    }
    intro.textContent = "数字必须在抓取的页面快照里逐字出现才会被采用。共 " +
      (d.counts.sources || 0) + " 个来源，" + (d.counts.discarded || 0) + " 个已丢弃。";
    var html = "";
    (d.sources || []).forEach(function (s) {
      html += '<div class="source-card"><div class="head"><div style="min-width:0">' +
        '<div class="t">' + WB.esc(s.title) + "</div>" +
        '<div class="m">' + WB.esc(s.publisher) + " · 检索于 " + WB.esc(s.date) + "</div></div>" +
        '<span class="pill ok">已核对</span></div>' +
        (s.quote ? '<div class="quote">' + WB.esc(s.quote) + "</div>" : "") +
        (s.url ? '<div class="note"><a href="' + WB.esc(s.url) + '" target="_blank" rel="noopener">查看来源页面</a></div>' : "") +
        "</div>";
    });
    (d.discarded || []).forEach(function (s) {
      html += '<div class="source-card"><div class="head"><div style="min-width:0">' +
        '<div class="t">' + WB.esc(s.url || "未知来源") + "</div>" +
        '<div class="m">' + WB.esc(s.code) + "</div></div>" +
        '<span class="pill na">已丢弃</span></div>' +
        (s.detail ? '<div class="note">' + WB.esc(s.detail) + "</div>" : "") + "</div>";
    });
    list.innerHTML = html;
  };

  // ------------------------------------------------------------ 右栏：配图
  WB.renderImages = function () {
    var st = WB.state;
    var intro = document.getElementById("images-intro");
    var grid = document.getElementById("image-grid");
    var d = st.images;
    var c = d && d.counts ? d.counts : { t1: 0, t2: 0, t3: 0, t4: 0 };
    document.getElementById("tab-count-images").textContent =
      d && d.available ? (d.tiles || []).length : 0;
    if (!d || !d.available) {
      intro.textContent = d && d.reason ? d.reason : "这份演示没有配图记录。";
      grid.innerHTML = "";
      return;
    }
    intro.textContent = "实拍图先从 Pexels 找，找不到合适的才在本机生成，仍不合适就不配图。" +
      "共 " + c.t1 + " 张原稿图、" + c.t2 + " 张检索图、" + c.t3 + " 张本机生成、" + c.t4 + " 页未配图。";
    var html = "";
    (d.tiles || []).forEach(function (t) {
      var box = '<div class="box ' + t.kind + '">';
      if (t.kind === "photo" || t.kind === "gen") {
        box += t.path ? '<img src="/api/jobs/' + encodeURIComponent(st.currentJobId) +
          '/page-image/1.png" alt="" aria-hidden="true" style="display:none">' : "";
        box += '<span class="tag ' + t.kind + '">' + WB.esc(t.label) + "</span>";
      } else {
        box += '<span class="tag none">未配图，版式已补位</span>';
      }
      box += "</div>";
      html += '<div class="image-tile">' + box +
        '<div class="meta"><div class="t">' + WB.esc(t.scene_id) + "</div>" +
        '<div class="s">' + WB.esc(t.reason || "") + "</div>" +
        (t.license ? '<div class="lic">许可：' + WB.esc(t.license) +
          (t.creator ? " · " + WB.esc(t.creator) : "") + "</div>" : "") +
        "</div></div>";
    });
    grid.innerHTML = html;
  };

  // ------------------------------------------------------------ 右栏：预览
  WB.renderPreview = function () {
    var st = WB.state;
    var pages = st.pages || [];
    var empty = document.getElementById("preview-empty");
    var main = document.getElementById("preview-main");
    document.getElementById("tab-count-preview").textContent = pages.length;
    if (!pages.length) {
      empty.hidden = false; main.hidden = true;
      empty.querySelector("p").textContent = st.currentJobId
        ? "这份演示还没有渲染图。任务完成后会显示每一页的真实渲染结果。"
        : "生成完成后，这里会显示每一页的真实渲染图。";
      return;
    }
    empty.hidden = true; main.hidden = false;
    if (st.currentPage > pages.length) st.currentPage = pages.length;
    if (st.currentPage < 1) st.currentPage = 1;
    var cur = pages[st.currentPage - 1];
    document.getElementById("preview-img").src = cur.url;
    document.getElementById("preview-page-label").innerHTML =
      "第 <b>" + cur.page_no + "</b> 页 · 共 " + pages.length + " 页";
    // 缩略条
    var thumbs = document.getElementById("thumbs");
    thumbs.innerHTML = pages.map(function (p) {
      return '<button type="button" class="thumb' + (p.page_no === cur.page_no ? " is-current" : "") +
        '" data-page="' + p.page_no + '" role="tab" aria-selected="' + (p.page_no === cur.page_no) +
        '" aria-label="第 ' + p.page_no + ' 页"><span class="box"><img src="' + p.url +
        '" alt="第 ' + p.page_no + ' 页缩略图" loading="lazy"></span><span class="n">' + p.page_no + "</span></button>";
    }).join("");
    Array.prototype.forEach.call(thumbs.querySelectorAll(".thumb"), function (el) {
      el.addEventListener("click", function () {
        st.currentPage = parseInt(el.getAttribute("data-page"), 10);
        WB.renderPreview(); WB.renderPageCheck();
      });
    });
    WB.renderPageCheck();
  };

  WB.renderPageCheck = function () {
    var st = WB.state;
    var box = document.getElementById("page-check-rows");
    var checks = (st.checks && st.checks.rows) || [];
    if (!checks.length) {
      box.innerHTML = '<span class="k">状态</span><span>还没有检查记录。</span><span class="pill na">未开始</span>';
      return;
    }
    box.innerHTML = checks.map(function (r) {
      var pill = r.status === "OK" || r.status === "PASS" ? '<span class="pill ok">通过</span>'
        : r.status === "NOT_EVALUABLE" ? '<span class="pill na">未评估</span>'
        : '<span class="pill warn">未通过</span>';
      return '<span class="k">' + WB.esc(r.label) + "</span><span>" +
        WB.esc(r.reason || "") + "</span>" + pill;
    }).join("");
  };
})();
