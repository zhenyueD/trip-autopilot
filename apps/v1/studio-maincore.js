(function (root) {
  'use strict';
  const list = value => Array.isArray(value) ? value : [];
  const part = encodeURIComponent;
  const submissionKey = id => 'studio-submission:' + id;
  function activeGoal(snapshot, preferred) {
    const goals = list(snapshot?.goals).filter(goal => goal.status === 'active');
    return goals.find(goal => goal.id === preferred) || goals[0];
  }
  function draftFingerprint(text, materials = {}) {
    return JSON.stringify([text.trim(),materials.evidence_text || '',materials.links || [],materials.documents || [],materials.images || []]);
  }
  function draftOutcome(pending, snapshot, fingerprint) {
    if (!pending || pending.tripId !== snapshot?.trip?.id || pending.fingerprint !== fingerprint
        || !snapshot.trip.plan || snapshot.trip.revision <= pending.revision) return false;
    const run = list(snapshot.runs).find(value => value.id === pending.runId);
    if (run?.result?.error_code || !['completed','waiting'].includes(run?.status)
        || (run.status === 'waiting' && run.result?.outcome_status !== 'ready_for_user')) return false;
    const proposals = new Set(list(snapshot.operations).filter(op => op.run_id === pending.runId
      && op.name === 'propose_revision' && op.status === 'succeeded').map(op => op.result?.proposal_id).filter(Boolean));
    return list(snapshot.revisions).some(revision => revision.kind === 'apply'
      && proposals.has(revision.proposal_id) && revision.revision > pending.revision);
  }
  function createController({autopilot, fetch, storage, uuid, token, changed = () => {}}) {
    const client = autopilot.createClient({fetch,storage,uuid,token,changed});
    client.restore();
    let caps = null;
    const goal = () => activeGoal(client.state.snapshot,client.state.saved.activeGoal);
    const tripPath = suffix => '/trips/' + part(client.state.session.id) + suffix;
    function remember(data, text, materials, kind, revision) {
      if (!data?.run?.id || !client.state.session) return;
      const key=submissionKey(client.state.session.id), previous=list(client.state.saved.drafts[key]);
      client.saveDraft(key, [...previous.filter(value => value.kind !== kind),{tripId:client.state.session.id,
        runId:data.run.id,revision:revision || 0,kind,fingerprint:draftFingerprint(text,materials)}]);
    }
    return {
      client, get enabled() { return caps?.enabled === true; },
      get modelReady() { return caps?.enabled === true && caps?.model_enabled === true; },
      get known() { return caps !== null; }, get active() { return Boolean(goal()); },
      get running() { return list(client.state.snapshot?.runs).some(run => ['pending','running'].includes(run.status)); },
      get busy() { return client.state.busy; }, goal,
      run() { return list(client.state.snapshot?.runs).find(run => run.goal_id === goal()?.id) || list(client.state.snapshot?.runs)[0]; },
      select(session) {
        if (client.state.session?.id === session?.id) return;
        client.attach(session || null);
      },
      async capabilities() {
        const reader = autopilot.createClient({fetch,storage:{getItem:() => null,setItem() {}},uuid});
        caps = await reader.refresh(); changed(client.state); return caps;
      },
      async refresh() {
        const data = await client.refresh();
        if (data?.capabilities) caps = data.capabilities;
        return data;
      },
      async start(payload, options = {}) {
        if (!this.modelReady) throw new Error('旅行助手尚未启用模型，需求与材料草稿仍保留。');
        const data = await client.mutate('/trips',{...payload,
          objective:payload.brief || '请根据提供的旅行材料，整理可执行安排，并确认会改变方案的未知条件。',
          auto_apply:options.auto_apply === true,allow_background_check:false,
          public_search_entities:options.public_search_entities || []},{newTrip:true});
        remember(data,payload.brief,payload,'start',0); return data;
      },
      async followup(payload) {
        if (!this.modelReady) throw new Error('旅行助手尚未启用模型，本次输入与材料草稿仍保留。');
        const {feedback = '',...materials} = payload;
        const request=feedback || '请结合新材料继续准备这趟旅行。';
        const previous=list(client.state.snapshot?.goals)[0];
        const data = goal() ? await client.mutate('/goals/'+part(goal().id)+'/events',{request,...materials})
          : await client.mutate(tripPath('/goals'),{objective:request,...materials,auto_apply:false,
            allow_background_check:false,public_search_entities:list(previous?.constraints?.public_search_entities)});
        remember(data,feedback,materials,'revision',payload.expected_revision); return data;
      },
      pending() { return list(client.state.saved.drafts[submissionKey(client.state.session?.id)]); },
      clearPending(runId) {
        const key=submissionKey(client.state.session?.id);
        client.saveDraft(key,list(client.state.saved.drafts[key]).filter(value => value.runId !== runId));
      },
      apply(proposal) { return client.mutate(tripPath('/apply'),{proposal_id:proposal.id || proposal.proposal_id,expected_revision:proposal.expected_revision}); },
      undo(revision) { return client.mutate(tripPath('/undo'),{revision_id:revision.revision_id || revision.id,expected_revision:client.state.snapshot.trip.revision}); },
      answer(question, answer) { return client.mutate(tripPath('/inputs/'+part(question.id)+'/answer'),{answer},{clearDraft:'studio-answer:'+client.state.session.id+':'+question.id}); },
      confirm() { return client.mutate('/goals/'+part(goal().id)+'/confirm',{expected_goal_version:goal().version}); }
    };
  }
  function mount({doc, win, container, changed = () => {}, updated = async () => {}}) {
    const autopilot = win.TravelAutopilot;
    const el = (tag, content, cls) => { const node=doc.createElement(tag); if(content) node.textContent=content; if(cls) node.className=cls; return node; };
    let controller, signature = '', questionSignature = '';
    let storage;
    try { storage=win.localStorage; } catch { storage={getItem() {throw new Error('storage unavailable');},setItem() {throw new Error('storage unavailable');}}; }
    controller = createController({autopilot,fetch:win.fetch.bind(win),storage,uuid:() => win.crypto.randomUUID(),
      changed() { if(controller) {render(); changed(controller);} }});
    const summary=el('div'), questions=el('div',null,'maincore-questions'), proposals=el('div'), detail=el('details'), history=el('div');
    const error=el('p',null,'maincore-error'); error.setAttribute('role','status');
    detail.append(el('summary','查看依据与实际进展'),history);
    container.append(summary,questions,proposals,detail,error);
    async function perform(action) {
      try { await action(); await updated(); }
      catch(error) { controller.client.state.error=error.message || '这次操作未确认，已有内容仍保留。'; }
      render(); changed(controller);
    }
    function button(label, action) {
      const node=el('button',label,'secondary-button'); node.type='button';
      node.dataset.maincoreMutation='true'; node.disabled=controller.busy || controller.running;
      node.addEventListener('click',() => perform(action)); return node;
    }
    function changes(parent, values) {
      list(values).forEach(change => {
        const before=autopilot.changeValue(change.before), after=autopilot.changeValue(change.after);
        const label=autopilot.readable(change.label || change.summary) || '安排调整';
        if(before || after) parent.append(el('p',label+'：'+before+' → '+after));
      });
    }
    function renderResult(snapshot) {
      const goal=controller.goal() || list(snapshot.goals).find(value => value.id === controller.client.state.saved.activeGoal) || list(snapshot.goals)[0];
      const runs=list(snapshot.runs).filter(run => run.goal_id === goal?.id);
      const run=runs[0], result=run?.result;
      summary.replaceChildren(el('h2','一起把旅行安排好'));
      if(goal) summary.append(el('p',goal.objective,'maincore-objective'));
      if(run) summary.append(el('p',autopilot.status(run.status),'field-hint'));
      if(result?.user_summary || result?.summary) summary.append(el('p',result.user_summary || result.summary));
      else if(run) summary.append(el('p',controller.running ? '目标已保存，实际进展会在这里更新。已有安排可以继续阅读。' : '本轮还没有可读结果。已保存的旅行与草稿仍保留。'));
      if(result?.receipt) summary.append(el('p',autopilot.receiptSummary(result.receipt)));
      list(result?.unresolved).forEach(issue => { const text=autopilot.readable(issue); if(text) summary.append(el('p',text,'field-hint')); });
      const fixture=autopilot.fixtureNotice(controller.client.state.capabilities);
      if(fixture) summary.append(el('p',fixture,'field-hint'));
      if(controller.active && ['complete','ready_for_user'].includes(result?.outcome_status)) summary.append(button('这份结果满足我的目标',() => controller.confirm()));
    }
    function renderQuestions(snapshot) {
      const pending=list(snapshot.workspace?.inputs).filter(q => q.status === 'pending');
      const next=JSON.stringify([snapshot.trip?.id,pending]);
      if(next===questionSignature) return; questionSignature=next;
      questions.replaceChildren(...pending.map(question => {
        const form=el('form',null,'maincore-question'), label=el('label',question.question), input=el('textarea');
        input.id='maincore-answer-'+question.id; label.htmlFor=input.id; input.rows=2; input.required=true; input.maxLength=6000;
        const key='studio-answer:'+snapshot.trip.id+':'+question.id;
        input.value=controller.client.state.saved.drafts[key] || '';
        input.addEventListener('input',() => controller.client.saveDraft(key,input.value));
        form.append(label); if(question.reason) form.append(el('p',question.reason,'field-hint'));
        const options=autopilot.questionOptions(doc,question.options,input,value => controller.client.saveDraft(key,value),() => controller.busy || controller.running);
        if(options) form.append(options);
        const submit=el('button','提交这个选择','primary-button'); submit.type='submit'; submit.dataset.maincoreMutation='true';
        form.append(input,submit); form.addEventListener('submit',event => {event.preventDefault(); perform(() => controller.answer(question,input.value.trim()));});
        return form;
      }));
    }
    function renderProposals(snapshot) {
      const revisions=list(snapshot.revisions);
      proposals.replaceChildren(...list(snapshot.proposals).map(proposal => {
        const id=proposal.id || proposal.proposal_id, applied=revisions.find(rev => rev.proposal_id === id && rev.kind === 'apply');
        const undone=applied && revisions.some(rev => rev.undo_of === (applied.revision_id || applied.id));
        const card=el('article',null,'maincore-proposal');
        card.append(el('h3',proposal.summary || proposal.plan?.title || '行程调整提案'));
        changes(card,proposal.changes || proposal.diff);
        if (proposal.plan) {
          const preview=el('details'); preview.append(el('summary','先看完整安排'));
          if(proposal.plan.summary) preview.append(el('p',autopilot.readable(proposal.plan.summary)));
          list(proposal.plan.days).forEach(day => {
            preview.append(el('h3',day.title || day.date || '每日安排'));
            list(day.items).forEach(item => preview.append(el('p',[item.time,item.title || item.name,item.description || item.note].filter(Boolean).join(' · '))));
          });
          card.append(preview);
        }
        if(applied) card.append(el('p',undone ? '这次调整已撤销' : '已应用到下方行程','field-hint'));
        else if(proposal.expected_revision !== snapshot.trip?.revision) card.append(el('p','方案已有更新，请基于最新版重新准备。','field-hint'));
        else if(proposal.valid_until && proposal.valid_until * 1000 <= Date.now()) card.append(el('p','这份提案已过期，请重新核对。','field-hint'));
        else if(['proposed','pending',undefined].includes(proposal.status)) card.append(button('确认应用这份安排',() => controller.apply(proposal)));
        if(applied && !undone && autopilot.undoAvailability(applied).available) card.append(button('撤销这次调整',() => controller.undo(applied)));
        return card;
      }));
    }
    function renderHistory(snapshot) {
      const work=list(snapshot.workspace?.work).map(item => el('p',(item.title || '旅行准备事项')+' · '+autopilot.status(item.status)));
      const names={search_sources:'查找公开资料',inspect_materials:'核对已有材料',read_source:'阅读来源原文',propose_revision:'准备行程提案',
        apply_revision:'应用已授权方案',check_constraints:'核对目标与结果',request_input:'整理需要你的选择',delegate:'整理证据与反例'};
      const operations=list(snapshot.operations).map(operation => {
        const row=el('p',(names[operation.name] || '旅行准备操作')+' · '+autopilot.status(operation.status));
        const summary=operation.result?.user_summary || operation.result?.summary;
        if(typeof summary === 'string') row.append(el('span',' · '+summary));
        return row;
      });
      const sourceRows=list(snapshot.sources || snapshot.trip?.material_sources || snapshot.trip?.materials);
      const unavailable=win.TravelPreparation.unavailableSourceURLs(sourceRows);
      const sources=sourceRows.map(source => {
        const url=autopilot.safeURL(source.url || source.source_url);
        const row=el('details'), withdrawn=source.revoked || ['withdrawn','retracted','invalidated','revoked'].includes(source.status) || source.availability === 'withdrawn';
        const blocked=withdrawn || unavailable.has(win.TravelPreparation.urlKey(url)) || source.unavailable === true || source.status === 'unavailable';
        row.append(el('summary',source.title || source.label || source.name || '旅行材料'));
        if(blocked) {row.append(el('p',withdrawn ? '材料已撤回，不能作为当前依据。' : '材料当前不可用，暂不提供来源跳转。'));return row;}
        if(url) {const link=el('a','查看原始来源');link.href=url;link.target='_blank';link.rel='noopener noreferrer';row.append(link);}
        if(source.content) row.append(el('p',source.content));
        if(source.message || source.summary) row.append(el('p',autopilot.readable(source.message || source.summary)));
        return row;
      });
      const revisions=list(snapshot.revisions).map(revision => {
        const row=el('article');row.append(el('h3',revision.kind === 'undo' ? '已撤销指定调整' : revision.summary || '行程变更回执'));
        changes(row,revision.changes || revision.diff);return row;
      });
      history.replaceChildren(...work,...operations,...sources,...revisions);
      detail.hidden=!history.childNodes.length;
    }
    function render() {
      const state=controller.client.state, snapshot=state.snapshot;
      container.hidden=!snapshot || (!list(snapshot.goals).length && !list(snapshot.runs).length);
      error.textContent=state.error || (state.storageFailed ? '本机存储失败，请保留当前页面并备份草稿。'
        : controller.known && !controller.modelReady ? '旅行助手尚未启用模型，已有安排与草稿保留，暂不提交新的输入。' : ''); error.hidden=!error.textContent;
      if(snapshot) {
        const next=JSON.stringify(snapshot);
        if(next!==signature) {signature=next;renderResult(snapshot);renderQuestions(snapshot);renderProposals(snapshot);renderHistory(snapshot);}
      }
      container.querySelectorAll('button,textarea').forEach(node => {node.disabled=controller.busy || controller.running || !controller.modelReady;});
    }
    return Object.assign(controller,{render,perform});
  }
  const api={createController,draftFingerprint,draftOutcome,activeGoal,mount};
  if(typeof module !== 'undefined' && module.exports) module.exports=api;
  else root.TravelMaincore=api;
})(typeof window === 'undefined' ? globalThis : window);
