import { FilterTasksPipe } from './filter-tasks.pipe';
import {
  PostprocessorStatus,
  RunningStatus,
  TaskData,
} from '../task.model';

describe('FilterTasksPipe', () => {
  it('create an instance', () => {
    const pipe = new FilterTasksPipe();
    expect(pipe).toBeTruthy();
  });

  it('filters recording rooms by fuzzy text and live start date', () => {
    const data = {
      user_info: { name: '学习主播' },
      room_info: {
        title: '数学直播',
        area_name: '教育学习',
        room_id: 100,
        short_room_id: 0,
        live_start_time: Math.floor(new Date(2026, 6, 15, 10).getTime() / 1000),
      },
      task_status: {
        running_status: RunningStatus.WAITING,
        postprocessor_status: PostprocessorStatus.WAITING,
      },
    } as TaskData;
    const pipe = new FilterTasksPipe();

    expect(
      pipe.transform([data], '学习', [
        new Date(2026, 6, 15),
        new Date(2026, 6, 15),
      ])
    ).toEqual([data]);
    expect(
      pipe.transform([data], '', [
        new Date(2026, 6, 16),
        new Date(2026, 6, 16),
      ])
    ).toEqual([]);
  });
});
