import React, {useState} from 'react';

// Matches the canonical province names already supported by bokjiro_collector.
const provinces=['서울특별시','부산광역시','대구광역시','인천광역시','광주광역시','대전광역시','울산광역시','세종특별자치시','경기도','강원특별자치도','충청북도','충청남도','전북특별자치도','전라남도','경상북도','경상남도','제주특별자치도'];
export default function ConditionRegionPicker({value,onChange}) {
    const initial=provinces.find(p=>value===p || value.startsWith(p+' '));
    const [province,setProvince]=useState(value==='전국'?'전국':initial || '직접 입력');
    const [district,setDistrict]=useState(initial?value.slice(initial.length).trim():'');
    return <div className="space-y-3">
        <label className="block text-sm font-medium">시·도<select aria-label="시·도" className="ui-field mt-2 font-normal" value={province} onChange={e=>{
            const next=e.target.value;setProvince(next);setDistrict('');onChange(next==='직접 입력'?'':next);
        }}><option>전국</option>{provinces.map(p=><option key={p}>{p}</option>)}<option>직접 입력</option></select></label>
        {province==='직접 입력'?<label className="block text-sm font-medium">찾을 지역<input className="ui-field mt-2 font-normal" maxLength={100} value={value} onChange={e=>onChange(e.target.value)} placeholder="예: 서울특별시 도봉구"/></label>:province!=='전국'&&<label className="block text-sm font-medium">시·군·구 (선택)<input className="ui-field mt-2 font-normal" maxLength={60} value={district} onChange={e=>{setDistrict(e.target.value);onChange(`${province} ${e.target.value}`.trim());}} placeholder="예: 도봉구 · 비워두면 선택한 시·도 전체"/></label>}
        <p className="text-sm text-gray-500">{province==='전국'?'등록된 수집처에서 지역 구분 없이 찾습니다.':'기관 주소가 아닌 실제 지원·참여 대상 지역을 확인합니다. 세부 지역명은 정확히 입력해주세요.'}</p>
    </div>;
}
