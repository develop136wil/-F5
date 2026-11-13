import os
import time
import logging
import sys

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import site_analyzer
import utils

# Setup logging
log_dir = 'logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, 'batch_add.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Target sites list provided by user
RAW_SITES = [
    {"name": "국민건강보험 - 새소식", "url": "https://www.nhis.or.kr/nhis/HealthInfo/NewsList.do"},
    {"name": "국민건강보험 - 공지사항", "url": "https://www.nhis.or.kr/nhis/board/list.do?bid=30"},
    {"name": "도봉보건소 - 공지사항", "url": "https://health.dobong.go.kr/bbs.asp?code=10005006"},
    {"name": "도봉보건소 - 채용/공고", "url": "https://health.dobong.go.kr/bbs.asp?code=10007922"},
    {"name": "복지상담센터 - 공지사항", "url": "https://www.129.go.kr/"},
    {"name": "복지로 - 새소식", "url": "https://www.bokjiro.go.kr/ssis-tbu/board/board.do?bbs_id=BBS_0000001"},
    {"name": "복지로 - 공지사항", "url": "https://www.bokjiro.go.kr/ssis-tbu/board/board.do?bbs_id=BBS_0000002"},
    {"name": "서울아이발달센터 - 공지사항", "url": "https://www.seoul-i.kr/board/notice"},
    {"name": "서울아이발달센터 - 자료실", "url": "https://www.seoul-i.kr/board/library"},
    {"name": "도봉구육아센터 - 공지사항", "url": "https://www.doccic.go.kr/bbs/board.php?bo_table=notice"},
    {"name": "도봉구육아센터 - 센터소식", "url": "https://www.doccic.go.kr/bbs/board.php?bo_table=news"},
    {"name": "도봉구 가족센터 - 공지사항", "url": "https://dobong.familynet.or.kr/center/"},
    {"name": "도봉구 가족센터 - 프로그램홍보", "url": "https://dobong.familynet.or.kr/center/lay1/program/S295T322C449/receipt/list.do"},
    {"name": "사회서비스 전자바우처 - 공지사항", "url": "https://www.socialservice.or.kr/user/bbs/list.do?bbs_id=BBS_0000022"},
    {"name": "서울북부특수교육센터 - 공지사항", "url": "https://sedu.sen.go.kr/bukbu/bbs/selectBbsList.do?bbsId=BBS_0000020"},
    {"name": "서울북부특수교육센터 - 자료실", "url": "https://sedu.sen.go.kr/bukbu/bbs/selectBbsId=BBS_0000021"},
    {"name": "보건복지부 - 새소식", "url": "https://www.mohw.go.kr/menu.es?mid=a10710010000"},
    {"name": "보건복지부 - 공지사항", "url": "https://www.mohw.go.kr/board.es?mid=a10501000000&bid=0003"},
    {"name": "보건복지부 - 보도자료", "url": "https://www.mohw.go.kr/menu.es?mid=a10503000000"},
    {"name": "도봉장애인복지관 - 공지사항", "url": "http://db.dfsc.or.kr/bbs/board.php?bo_table=notice"},
    {"name": "도봉장애인복지관 - 복지관소식", "url": "http://db.dfsc.or.kr/bbs/board.php?bo_table=news"},
    {"name": "아이돌봄서비스 - 공지사항", "url": "https://idolbom.go.kr/front/board/boardList.do?boardId=BBS_0000001"},
    {"name": "서울시장애인부모회 - 공지사항", "url": "https://www.seoulbumo.co.kr/board/bbs/board.php?bo_table=notice01"},
    {"name": "서울복지포털 - 공지사항", "url": "https://wis.seoul.go.kr/news/notice/list.do"},
    {"name": "서울복지포털 - 복지소식", "url": "https://wis.seoul.go.kr/news/welfare-news/list.do"},
    {"name": "몽땅정보만능키 - 새소식/공지", "url": "https://umppa.seoul.go.kr/hmpg/main.do"},
    {"name": "중앙장애아동센터 - 공지사항", "url": "https://www.broso.or.kr/user/board/boardListPage.do?boardId=1"},
    {"name": "중앙장애아동센터 - 센터소식", "url": "https://www.broso.or.kr/user/board/boardListPage.do?boardId=4"},
    {"name": "서울 맘케어 - 공지사항", "url": "https://www.seoulmomcare.com/notice/userNoticeList.do"},
    {"name": "장애인활동지원 - 공지사항", "url": "https://www.ableservice.or.kr/"},
    {"name": "도봉구청 - 고시/공고", "url": "https://www.dobong.go.kr/Contents.asp?code=10008773"},
    {"name": "도봉구청 - 도봉소식", "url": "https://www.dobong.go.kr/wdb_dev/MC0101/MC0101List.asp"},
    {"name": "서울특별시교육청 - 공지사항", "url": "https://www.sen.go.kr/user/bbs/BD_selectBbsList.do?q_bbsSn=1090"},
    {"name": "서울특별시교육청 - 보도/해명", "url": "https://www.sen.go.kr/user/bbs/BD_selectBbsList.do?q_bbsSn=1070"},
    {"name": "서울시복지재단 - 재단소식", "url": "https://www.welfare.seoul.kr/web/contents/communication1-1.do"}
]

def main():
    logger.info("🚀 Batch Site Addition Started...")
    logger.info("ℹ️ Note: This process will take some time due to API rate limits.")
    
    existing_sites = utils.load_sites()
    existing_urls = [s['url'] for s in existing_sites]
    
    added_count = 0
    failed_count = 0
    
    for i, target in enumerate(RAW_SITES):
        logger.info(f"[{i+1}/{len(RAW_SITES)}] Processing: {target['name']}")
        
        if target['url'] in existing_urls:
            logger.warning("Already exists, skipping.")
            continue
            
        max_retries = 3
        retry_delay = 60
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Analyzing {target['url']} (Attempt {attempt+1})...")
                result = site_analyzer.analyze_site(target['url'])
                
                if result.get('success'):
                    new_site = {
                        "name": target['name'],
                        "url": target['url'],
                        "selectors": {
                            "list_item": result.get('list_item', 'table tbody tr'),
                            "title": result.get('title', 'td a'),
                            "date": result.get('date', 'td:nth-child(3)'),
                            "link": result.get('link', result.get('title', 'td a'))
                        },
                        "base_url": result.get('base_url', '')
                    }
                    
                    existing_sites.append(new_site)
                    utils.save_sites(existing_sites)
                    logger.info(f"✅ Added successfully! (Confidence: {result.get('confidence', 'Unknown')})")
                    added_count += 1
                    break
                else:
                    error_msg = result.get('error', '')
                    if "429" in error_msg or "Quota" in error_msg or "ResourceExhausted" in error_msg:
                        logger.warning(f"Rate limit hit. Waiting {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        retry_delay *= 2 # Exponential backoff
                        continue
                    else:
                        logger.error(f"Analysis failed: {error_msg}")
                        failed_count += 1
                        break
                    
            except Exception as e:
                logger.error(f"Critical Error: {e}")
                failed_count += 1
                break
        
        # Default rate limiting
        logger.info("Waiting 10 seconds to respect API limits...")
        time.sleep(10)
        
    logger.info(f"✨ Completed! Added: {added_count}, Failed/Skipped: {failed_count}")

if __name__ == "__main__":
    main()
