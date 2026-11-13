// 결과가 성공이어도 설정이 바뀌었다면 완료 표시로 재사용하지 않는다.
export const currentVerification = (revision, ...checks) =>
    checks.find(check => Boolean(revision) && check?.revision === revision);
