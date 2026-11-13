import {useQuery} from '@tanstack/react-query';
import {welfareApi} from '../api/welfare';
import {newsTaskPollInterval} from '../utils/newsTaskProgress';
import {readNewsProfiles,readNewsWorkflowHistory} from '../utils/newsResponse';

export const useNewsTaskStatus=()=>useQuery({
    queryKey:['global-news-task-status'],
    queryFn:async({signal})=>{
        const profiles=readNewsProfiles(await welfareApi.getNewsProfiles(signal));
        const results=[];
        for(const profile of profiles.items||[]){
            if(profile.archived)continue;
            const history=readNewsWorkflowHistory(await welfareApi.getNewsWorkflows(profile.profile_id,signal));
            for(const [index,item] of (history.items||[]).entries()){
                if(item.active||['running','ready'].includes(item.state)||(index===0&&['waiting','failed'].includes(item.state)))
                    results.push({...item,profile_id:profile.profile_id,profileName:profile.name});
            }
        }
        return results;
    },
    refetchInterval:query=>newsTaskPollInterval(query.state.data),refetchIntervalInBackground:false,staleTime:2000,retry:false,
});
