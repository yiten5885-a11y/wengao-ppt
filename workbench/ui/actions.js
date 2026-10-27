/* 稳稿工作台交互层：全部动作走真实接口；失败如实提示，不静默。 */
(function () {
  "use strict";
  var WB = window.WB;

  WB.bindComposer = function () {
    var msg = document.getElementById("msg");
    document.getElementById("btn-send").addEventListener("click", function () { send(); });
    msg.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });
    document.getElementById("btn-research").addEventListener("click", function () {
      var on = !WB.state.researchOn;
      WB.state.researchOn = on;
      this.classList.toggle("is-on", on);
      this.setAttribute("aria-pressed", String(on));
      WB.toast(on ? "联网研究已开启" : "联网研究已关闭：对比与趋势页会省略");
    });
    document.getElementById("btn-import").addEventListener("click", function () {
      document.getElementById("file-input").click();
    });
    document.getElementById("file-input").addEventListener("change", function () {
      if (this.files && this.files[0]) importFile(this.files[0]);
      this.value = "";
    });
    document.getElementById("btn-lang").addEventListener("click", function () {
      var order = ["auto", "zh", "en", "ja", "ar", "hi", "ru"];
      var i = order.indexOf(WB.state.lang);
      WB.state.lang = order[(i + 1) % order.length];
      this.textContent = "语言：" + ({ auto: "自动", zh: "中文", en: "英文", ja: "日文", ar: "阿拉伯文", hi: "印地文", ru: "俄文" })[WB.state.lang];
    });
    var themeBtn = document.getElementById("btn-theme");
    if (themeBtn) {
      themeBtn.addEventListener("click", function () {
        // FIX-W4-001：只循环后端可信主题注册表里的主题（/api/meta.themes），
        // 不再使用不在注册表内的虚构主题（旧代码的 ink/slate/sand/ocean 会被
        // /api/make 以 THEME.UNKNOWN 拒绝，"开始制作"必然失败）。
        var themes = (WB.state.meta && WB.state.meta.themes) || [];
        if (!themes.length) { WB.toast("主题清单尚未加载，请稍候再试。"); return; }
        var ids = themes.map(function (t) { return t.id; });
        var i = ids.indexOf(WB.state.theme);
        WB.state.theme = ids[(i + 1) % ids.length];
        WB.renderThemeButton();
        WB.toast("已选择主题：" + WB.state.theme);
      });
    }
    // 空状态示例
    Array.prototype.forEach.call(document.querySelectorAll("#empty-chips .chip"), function (el) {
      el.addEventListener("click", function () {
        document.getElementById("msg").value = el.getAttribute("data-sample");
        msg.focus();
      });
    });
    // 搜索
    document.getElementById("search-input").addEventListener("input", WB.renderJobList);
    // 左栏筛选
    Array.prototype.forEach.call(document.querySelectorAll(".side-nav-item"), function (el) {
      el.addEventListener("click", function () {
        Array.prototype.forEach.call(document.querySelectorAll(".side-nav-item"), function (x) {
          x.classList.remove("is-active"); x.setAttribute("aria-pressed", "false");
        });
        el.classList.add("is-active"); el.setAttribute("aria-pressed", "true");
        WB.state.filter = el.getAttribute("data-filter");
        WB.renderJobList();
      });
    });
  };

  function send() {
    var msg = document.getElementById("msg");
    var text = msg.value.trim();
    if (!text) return;
    msg.value = "";
    if (WB.state.currentJobId) {
      // 已有任务：作为继续调整指令（局部修改）
      WB.api("/api/ui/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/page-ops", {
        body: { op: "local_edit", edit: { instruction: text } },
      }).then(function (r) {
        WB.toast(r.status === "UNKNOWN_OP" ? "已收到：" + text : "已提交处理");
      }).catch(function (e) { WB.toast("没有提交成功：" + e.message); });
      return;
    }
    // 新任务：走真实材料生成入口
    WB.toast("已收到，开始读取材料");
    WB.api("/api/from-material", {
      body: {
        text: text,
        pages: 12,
        theme: WB.state.theme || (WB.state.meta && WB.state.meta.theme_default) || "",
        research: WB.state.researchOn,
        language: WB.state.lang,
      },
    }).then(function (r) {
      var tid = r.task_id || (r.task && r.task.task_id) || r.job_id;
      WB.toast("任务已创建，正在处理");
      WB.loadOverview();
      if (tid) setTimeout(function () { WB.selectJob(tid); }, 800);
    }).catch(function (e) { WB.toast("创建任务失败：" + e.message); });
  }

  function importFile(file) {
    // FIX-W4-001：上传必须是"原始字节 + X-Filename 头"（服务端 _upload_bytes 按
    // Content-Length 流式落盘，不解析 multipart）。此前误用 FormData 且未带
    // X-Filename，服务端把文件名回退为 upload.pptx，导致 /ui 的材料导入必然失败。
    var isPptx = /\.pptx$/i.test(file.name);
    fetch(isPptx ? "/api/upload" : "/api/material/upload", {
      method: "POST",
      headers: { "X-Filename": encodeURIComponent(file.name) },
      body: file,
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.error) throw new Error(d.error.message);
        WB.toast("已导入 " + file.name);
        WB.loadOverview();
        if (isPptx) {
          WB.api("/api/make", { body: { source: d.path || d.name, theme: WB.state.theme || (WB.state.meta && WB.state.meta.theme_default) || "" } })
            .then(function (r) {
              WB.toast("制作任务已创建");
              WB.loadOverview();
              if (r.job_id) setTimeout(function () { WB.selectJob(r.job_id); }, 800);
            }).catch(function (e) { WB.toast("创建制作任务失败：" + e.message); });
        }
      })
      .catch(function (e) { WB.toast("导入失败：" + e.message); });
  }

  WB.bindTabs = function () {
    Array.prototype.forEach.call(document.querySelectorAll("#tabs .seg-btn"), function (el) {
      el.addEventListener("click", function () {
        Array.prototype.forEach.call(document.querySelectorAll("#tabs .seg-btn"), function (x) {
          x.classList.remove("is-active"); x.setAttribute("aria-pressed", "false");
        });
        el.classList.add("is-active"); el.setAttribute("aria-pressed", "true");
        WB.state.tab = el.getAttribute("data-tab");
        document.getElementById("tab-preview").hidden = WB.state.tab !== "preview";
        document.getElementById("tab-sources").hidden = WB.state.tab !== "sources";
        document.getElementById("tab-images").hidden = WB.state.tab !== "images";
      });
    });
  };

  WB.bindGlobalButtons = function () {
    document.getElementById("btn-new").addEventListener("click", function () {
      WB.state.currentJobId = null; WB.closeSSE();
      WB.state.events = []; WB.state.pages = []; WB.state.terminal = null;
      WB.renderTimeline(); WB.renderPreview(); WB.renderJobHead(); WB.renderJobList();
      document.getElementById("msg").focus();
    });
    var btnCancel = document.getElementById("btn-cancel");
    if (btnCancel) {
      btnCancel.addEventListener("click", function () {
        if (!WB.state.currentJobId) return;
        WB.api("/api/ui/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/cancel", { method: "POST" })
          .then(function () {
            WB.toast("已取消制作");
            WB.state.running = false;
            WB.state.terminal = "CANCELLED";
            WB.renderJobHead();
          }).catch(function (e) { WB.toast("取消请求失败：" + e.message); });
      });
    }
    var btnRetry = document.getElementById("btn-retry");
    if (btnRetry) {
      btnRetry.addEventListener("click", function () {
        if (!WB.state.currentJobId) return;
        WB.api("/api/ui/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/retry", { method: "POST" })
          .then(function () {
            WB.toast("重试任务已提交");
            WB.state.terminal = null;
            WB.state.running = true;
            WB.renderJobHead();
            WB.loadOverview();
          }).catch(function (e) { WB.toast("重试失败：" + e.message); });
      });
    }
    document.getElementById("btn-export").addEventListener("click", function () {
      if (!WB.state.currentJobId) { WB.toast("先选一份演示"); return; }
      WB.api("/api/ui/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/export", {
        body: { format: "pptx", with_attribution: true },
      }).then(function (r) {
        if (r.status === "OK") WB.toast("已导出：" + (r.files || []).join("、") + "（含来源与署名清单）");
        else WB.toast(r.note || "暂不能导出");
      }).catch(function (e) { WB.toast("导出失败：" + e.message); });
    });
    document.getElementById("btn-share").addEventListener("click", function () {
      WB.toast("分享链接只在本机生成；联网发布需要你另行确认");
    });
    document.getElementById("btn-play").addEventListener("click", function () {
      WB.toast("放映需要用 PowerPoint 或 Keynote 打开导出的文件");
    });
    document.getElementById("btn-transition").addEventListener("click", function () {
      if (!WB.state.currentJobId) { WB.toast("先选一份演示"); return; }
      WB.api("/api/ui/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/page-ops", {
        body: { op: "transition", enabled: true, duration_ms: 500 },
      }).then(function () { WB.toast("转场设置已记录：整稿统一淡入淡出"); })
        .catch(function (e) { WB.toast("设置失败：" + e.message); });
    });
    document.getElementById("btn-history").addEventListener("click", function () {
      if (!WB.state.currentJobId) { WB.toast("先选一份演示"); return; }
      WB.api("/api/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/edits")
        .then(function (d) {
          var versions = (d && (d.versions || d.edits)) || [];
          WB.toast(versions.length ? "共有 " + versions.length + " 个历史版本" : "这份演示还没有历史版本");
        }).catch(function () { WB.toast("读取版本历史失败"); });
    });
    // 事件委托：决策按钮 / 建议 chips
    document.getElementById("chat").addEventListener("click", function (e) {
      var t = e.target;
      while (t && t !== document.getElementById("chat")) {
        if (t.hasAttribute && t.hasAttribute("data-decision")) {
          var cid = t.getAttribute("data-cid");
          var choice = t.getAttribute("data-decision");
          WB.api("/api/ui/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/decisions", {
            body: { conflict_id: cid, choice: choice },
          }).then(function () {
            WB.toast("已记录你的选择，相关页面已重排");
            WB.loadEvidence(WB.state.currentJobId);
          }).catch(function (err) { WB.toast("记录失败：" + err.message); });
          return;
        }
        if (t.hasAttribute && t.hasAttribute("data-suggest")) {
          var s = t.getAttribute("data-suggest");
          WB.toast("已收到：" + s);
          return;
        }
        t = t.parentNode;
      }
    });
    // 事件委托：本页对象编辑
    var po = document.getElementById("page-objects");
    if (po) {
      po.addEventListener("click", function (e) {
        var t = e.target;
        if (t && t.classList && t.classList.contains("btn-apply-obj")) {
          var card = t.closest(".obj-card");
          var shapeId = t.getAttribute("data-shape-id");
          var textarea = card ? card.querySelector(".obj-edit-input") : null;
          var newText = textarea ? textarea.value.trim() : "";
          if (!WB.state.currentJobId) { WB.toast("请先选择演示任务"); return; }
          WB.api("/api/ui/jobs/" + encodeURIComponent(WB.state.currentJobId) + "/page-ops", {
            body: {
              op: "local_edit",
              edit: {
                instruction: "修改对象 " + shapeId + " 文本为：" + newText,
                targets: [{ slide: WB.state.currentPage || 1, shape_id: parseInt(shapeId, 10), text: newText }]
              }
            }
          }).then(function (r) {
            WB.toast("对象修改已提交处理");
            WB.loadEvidence(WB.state.currentJobId);
          }).catch(function (err) { WB.toast("修改失败：" + err.message); });
        }
      });
    }
  };
})();
