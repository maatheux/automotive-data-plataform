from datetime import datetime, timedelta
from airflow.decorators import dag, task
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2026, 3, 3),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 0,
    'retry_delay': timedelta(minutes=5),
}

@dag(
    dag_id='postgres_to_snowflake',
    default_args=default_args,
    description='Load data incrementally from Postgres to Snowflake',
    schedule=timedelta(days=1),
    catchup=False,
    max_active_runs=1
)
def postgres_to_snowflake_etl():

    @task
    def get_max_primary_key(table_name: str):
        with SnowflakeHook(snowflake_conn_id='snowflake').get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f'SELECT MAX(ID_{table_name}) FROM {table_name}')
                max_id = cursor.fetchone()[0]
                return max_id if max_id is not None else 0
    
    @task
    def load_incremental_data(table_name: str, max_id: int):
        primary_key = f'ID_{table_name}'
        BATCH_SIZE = 10000
        
        with PostgresHook(postgres_conn_id='postgres').get_conn() as pg_conn:
            with pg_conn.cursor() as pg_cursor:

                pg_cursor.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table_name}'")
                columns = [row[0] for row in pg_cursor.fetchall()]
                columns_list_str = ', '.join(columns)
                placeholders = ', '.join(['%s'] * len(columns))

                pg_cursor.execute(f"SELECT {columns_list_str} FROM {table_name} WHERE {primary_key} > %s", (max_id,))
                
                sf_hook = SnowflakeHook(snowflake_conn_id='snowflake')
                sf_conn = sf_hook.get_conn()

                try:
                    sf_conn.auto_commit = False
                    total_inserted = 0

                    with sf_conn.cursor() as sf_cursor:
                        insert_query = f"INSERT INTO {table_name} ({columns_list_str}) VALUES ({placeholders})"
                        
                        while True:
                            rows = pg_cursor.fetchmany(BATCH_SIZE)
                            if not rows:
                                break

                            sf_cursor.executemany(insert_query, rows)
                            total_inserted += len(rows)
                    
                    if total_inserted == 0:
                        print(f"Nenhum dado novo para tabela {table_name}.")
                        sf_conn.rollback()
                        return
                    
                    sf_conn.commit()
                    print(f"{len(rows)} linhas inseridas em {table_name}.")
                
                except Exception as e:
                    sf_conn.rollback()
                    raise RuntimeError(f"Falha ao inserir em {table_name}: {e}") from e
                
                finally:
                    sf_conn.close()

    table_names = ['veiculos', 'estados', 'cidades', 'concessionarias', 'vendedores', 'clientes', 'vendas']

    for table_name in table_names:
        max_id = get_max_primary_key.override(task_id=f'get_max_id_{table_name}')(table_name)
        load_incremental_data.override(task_id=f'load_data_{table_name}')(table_name, max_id)
    

postgres_to_snowflake_etl_dag = postgres_to_snowflake_etl()
