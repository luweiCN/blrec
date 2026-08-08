import { Pipe, PipeTransform } from '@angular/core';

import { TaskData } from '../task.model';

export function* filter<T>(
  it: Iterable<T>,
  predicate: (value: T) => unknown
): Generator<T> {
  for (const item of it) {
    if (predicate(item)) {
      yield item;
    }
  }
}

@Pipe({
  name: 'filterTasks',
})
export class FilterTasksPipe implements PipeTransform {
  transform(
    dataList: Iterable<TaskData>,
    term: string = '',
    dateRange: Date[] | null = null
  ): TaskData[] {
    console.debug("filter tasks by '%s'", term);
    const normalizedTerm = term.trim().toLocaleLowerCase();
    let startedFrom: number | null = null;
    let startedTo: number | null = null;
    if (dateRange?.length === 2) {
      const from = new Date(dateRange[0]);
      from.setHours(0, 0, 0, 0);
      const to = new Date(dateRange[1]);
      to.setHours(23, 59, 59, 999);
      startedFrom = Math.floor(from.getTime() / 1000);
      startedTo = Math.floor(to.getTime() / 1000);
    }
    return [
      ...filter(dataList, (data) => {
        const liveStartTime = data.room_info.live_start_time;
        const matchesDate =
          startedFrom === null ||
          startedTo === null ||
          (liveStartTime >= startedFrom && liveStartTime <= startedTo);
        const values = [
          data.user_info.name,
          data.room_info.title,
          data.room_info.area_name,
          data.room_info.parent_area_name,
          data.room_info.room_id.toString(),
          data.room_info.short_room_id.toString(),
        ];
        return (
          matchesDate &&
          (normalizedTerm === '' ||
            values.some((value) =>
              value.toLocaleLowerCase().includes(normalizedTerm)
            ))
        );
      }),
    ];
  }
}
