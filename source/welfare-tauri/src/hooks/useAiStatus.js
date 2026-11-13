import { useQuery } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';

export const useAiStatus = () => useQuery({
    queryKey: ['ai-global-status'],
    queryFn: welfareApi.getAiStatus,
    // 작업 중에는 버튼 잠금/진행 안내를 빠르게 갱신하고, 유휴 중에는 불필요한 요청을 줄인다.
    refetchInterval: (query) => query.state.data?.busy ? 2000 : 8000,
    refetchIntervalInBackground: false,
    staleTime: 1500,
    retry: false,
});
