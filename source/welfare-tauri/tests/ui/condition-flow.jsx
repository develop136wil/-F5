// Isolated integration fixture: no backend, collection, or AI calls.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import '../../src/index.css';
import {welfareApi} from '../../src/api/welfare';
import NewsProfilesPanel from '../../src/components/NewsProfilesPanel';
window.fixture={items:[],creates:0,updates:0};
const db=window.fixture;
welfareApi.getNewsProfiles=async()=>({items:structuredClone(db.items)});
welfareApi.getNewsProfileTaxonomy=async()=>({life_cycles:['아동'],household_situations:['다문화·탈북민'],interest_topics:[],presets:[]});
welfareApi.getSites=async()=>[{id:1,name:'시험 기관'}];
welfareApi.createNewsProfile=async value=>{
 db.creates++;
 const profile={...structuredClone(value),profile_id:'created',revision:1};
 db.items.push(profile);return {profile_id:profile.profile_id};
};
welfareApi.getNewsProfile=async id=>({profile:structuredClone(db.items.find(p=>p.profile_id===id))});
welfareApi.saveNewsProfile=async(id,revision,value)=>{
 const profile=db.items.find(p=>p.profile_id===id);
 if(profile.revision!==revision)throw Error('버전 충돌');
 db.updates++;Object.assign(profile,structuredClone(value),{revision:revision+1});
 return {revision:profile.revision};
};
createRoot(document.getElementById('root')).render(<MemoryRouter><main className="max-w-5xl mx-auto p-6"><NewsProfilesPanel/></main></MemoryRouter>);
