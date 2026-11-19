(() => {
  'use strict';
  const form = document.getElementById('view-form'), proposal = document.getElementById('preview-form');
  const status = document.getElementById('status'), preview = document.getElementById('preview'), days = document.getElementById('days');
  let sequence = 0;
  const reasons = {OUTSIDE_INVENTORY_HORIZON:'部分入住日期无库存数据', NO_SAME_ROOM_FOR_ENTIRE_STAY:'没有整段连续可住的同一间房', PROTECTED_INVENTORY:'候选升级触及保护库存', BED_TYPE_INCOMPATIBLE:'候选床型不符', PARTY_OR_CHILD_REQUIREMENTS_UNSUPPORTED:'人数或儿童条件不满足', AUXILIARY_INVENTORY_EXHAUSTED:'加床或婴儿床不足', UPGRADE_COST_BUDGET_EXCEEDED:'升级成本超预算', NO_ELIGIBLE_ROOM_TYPE:'无符合策略的房型'};
  function params(source) { return [...new FormData(source)].filter(([,v]) => v !== ''); }
  function clear() { sequence++; days.replaceChildren(); preview.textContent='条件已改变，请重新读取或试算'; status.textContent='尚未刷新'; }
  form.addEventListener('input', clear); proposal.addEventListener('input', clear);
  async function load(withProposal) {
    const id=++sequence; days.replaceChildren(); preview.textContent=withProposal?'试算中…':'尚未试算'; status.textContent='读取快照中…';
    try {
      const query = new URLSearchParams([...params(form), ...(withProposal?params(proposal):[])]);
      const response=await fetch('/api/availability?'+query); const data=await response.json();
      if(id!==sequence)return;
      if(!response.ok)throw new Error(data.error?.message || '读取失败');
      status.textContent=`快照 ${data.snapshot_at} · ${data.mode} · 有效范围 ${data.horizon_start} 至 ${data.horizon_end}（不含）`;
      if(!form.elements.start.value)form.elements.start.value=data.horizon_start;
      for(const [name, values] of [['room_type',data.room_types],['bed',data.bed_types]]) {
        const select=proposal.elements[name], old=select.value; select.replaceChildren();
        values.forEach(value=>{const option=document.createElement('option');option.value=value;option.textContent=value;select.append(option);});
        if(values.includes(old))select.value=old;
      }
      data.days.forEach(day=>{const row=document.createElement('tr');
        const cells=[day.date,day.available?day.types.map(t=>`${t.room_type}: ${t.remaining} / ${t.active_protection}（${t.configured_protection}）`).join('；'):'无库存数据',day.available?`${day.extra_beds_remaining} / ${day.cots_remaining}`:'不可查询'];
        cells.forEach(text=>{const cell=document.createElement('td');cell.textContent=text;row.append(cell);}); days.append(row);
      });
      if(data.preview)preview.textContent=data.preview.accepted?`当前快照可分配：${data.preview.room_id}，${data.preview.nights.length}晚，加床${data.preview.extra_beds}，婴儿床${data.preview.cots}。未预留库存。`:`当前不可分配：${data.preview.rejection_reasons.map(r=>reasons[r]||r).join('；')}。这些是候选检查原因，非唯一因果结论。`;
    } catch(error) { if(id===sequence){days.replaceChildren();status.textContent='读取失败：'+error.message;preview.textContent='无有效试算结果';} }
  }
  form.addEventListener('submit',event=>{event.preventDefault();load(false);});
  proposal.addEventListener('submit',event=>{event.preventDefault();load(true);});
  load(false);
})();
