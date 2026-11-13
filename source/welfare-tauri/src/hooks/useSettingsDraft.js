import { useSyncExternalStore } from 'react';
import { settingsDrafts } from '../utils/settingsDrafts';

export function useSettingsDraft(name, initial) {
    settingsDrafts.ensure(name, initial);
    useSyncExternalStore(settingsDrafts.subscribe, settingsDrafts.getVersion);
    return [settingsDrafts.get(name), next => settingsDrafts.edit(name, next)];
}
export function useSettingsDirtyNames() {
    useSyncExternalStore(settingsDrafts.subscribe, settingsDrafts.getVersion);
    return settingsDrafts.dirtyNames();
}
