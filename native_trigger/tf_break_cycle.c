// MIT License
//
// Copyright (c) 2023 Ernst-Georg Schmid
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

#include "postgres.h"
#include "fmgr.h"
#include "executor/spi.h"       
#include "commands/trigger.h"   
#include "utils/rel.h"          
#include "utils/fmgrprotos.h"

PG_MODULE_MAGIC;

PGDLLEXPORT Datum tf_break_cycle(PG_FUNCTION_ARGS);

PG_FUNCTION_INFO_V1(tf_break_cycle);

static SPIPlanPtr  _plan = NULL;
static const Datum	false_val = BoolGetDatum(false);

Datum
tf_break_cycle(PG_FUNCTION_ARGS)
{
	TriggerData* trigdata = (TriggerData*)fcinfo->context;
	TupleDesc   tup_desc = trigdata->tg_relation->rd_att;
	HeapTuple   rettuple = NULL;
	bool        is_replicated, is_local, isnull, null_flag;
	int         ret, col_num;

	if (!CALLED_AS_TRIGGER(fcinfo))
		elog(ERROR, "tf_break_cycle: not called by trigger manager");

	if (TRIGGER_FIRED_BY_UPDATE(trigdata->tg_event)) {
		rettuple = trigdata->tg_newtuple;
	}
	else {
		rettuple = trigdata->tg_trigtuple;
	}

	if (TRIGGER_FIRED_AFTER(trigdata->tg_event))
		elog(ERROR, "tf_break_cycle must not fire AFTER");

	if ((ret = SPI_connect()) < 0)
		elog(ERROR, "tf_break_cycle: SPI_connect returned %d", ret);

	if (NULL == _plan) {
		_plan = SPI_prepare("SELECT pg_backend_pid() = ANY((SELECT pid FROM pg_stat_activity WHERE backend_type = 'logical replication worker'))::boolean", 0, NULL);

		if (_plan == NULL)
			elog(ERROR, "tf_break_cycle: SPI_prepare returned NULL");

		ret = SPI_keepplan(_plan);

		if (ret < 0)
			elog(ERROR, "tf_break_cycle: SPI_keepplan returned %d", ret);
	}

	ret = SPI_execute_plan(_plan, NULL, NULL, true, 1);

	if (ret < 0)
		elog(ERROR, "tf_break_cycle: SPI_execute_plan returned %d", ret);

	is_replicated = DatumGetBool(SPI_getbinval(SPI_tuptable->vals[0],
		SPI_tuptable->tupdesc,
		1,
		&isnull));

	col_num = SPI_fnumber(tup_desc, "is_local");

	is_local = DatumGetBool(SPI_getbinval(rettuple,
		tup_desc,
		col_num,
		&isnull));

	if (is_replicated) {
		if (is_local) {
			rettuple = SPI_modifytuple(trigdata->tg_relation, rettuple, 1, &col_num, &false_val, &null_flag);
		}
		else {
			rettuple = NULL;
		}
	}

	SPI_finish();

	return PointerGetDatum(rettuple);
}
