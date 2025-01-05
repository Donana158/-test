from quart import Quart, render_template, request, jsonify, websocket
from py2neo import Graph, ClientError
from werkzeug.utils import secure_filename
from openai import ChatCompletion
import requests
import os, re, uuid, openai, asyncio, aiohttp, json  
import boto3  

app = Quart(__name__)


# 連接到 Neo4j 資料庫

graph = Graph("bolt://10.0.139.25:7687", auth=("neo4j", "00000000"))

# GPT API 配置
GPT_API_URL = "https://api.openai.com/v1/chat/completions"  # GPT API Endpoint
GPT_API_KEY = "sk-proj-ZgYXVRHWmeavgesU2ZPuT3BlbkFJMylLjotIdSWb2jLfEhhx"  # API

# 定義上傳目錄
app.config['UPLOAD_FOLDER'] = './uploads'  
app.config['PROCESSED_FOLDER'] = './processed'  
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PROCESSED_FOLDER'], exist_ok=True)

# 初始化 S3 客戶端
s3 = boto3.client('s3', region_name='us-east-1')
bucket_name = 'txt-uploads'

def upload_to_s3_public(file_stream, bucket_name, filename):
    """從文件流上傳到 S3 公共 URL"""
    try:
        s3_url = f"https://{bucket_name}.s3.amazonaws.com/{filename}"

        # 上傳文件流
        response = requests.put(s3_url, data=file_stream)

        # 驗證上傳是否成功
        if response.status_code == 200:
            print(f"檔案已成功上傳到 S3：{s3_url}")
            return s3_url
        else:
            print(f"上傳失敗，狀態碼：{response.status_code}, 訊息：{response.text}")
            return None
    except Exception as e:
        print(f"上傳過程中出現錯誤：{e}")
        return None


def read_from_s3_public(s3_url):
    """從 S3 公共 URL 讀取文件內容"""
    try:
        response = requests.get(s3_url)
        if response.status_code == 200:
            print(f"成功從 S3 讀取文件內容：{s3_url}")
            return response.text  # 返回文件內容
        else:
            print(f"讀取失敗，狀態碼：{response.status_code}, 訊息：{response.text}")
            return None
    except Exception as e:
        print(f"讀取過程中出現錯誤：{e}")
        return None
    
async def call_gpt_api(prompt):
    """調用 GPT API 解析或生成內容"""
    headers = {
        "Authorization": f"Bearer {GPT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 5000,
        "temperature": 0.5
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(GPT_API_URL, headers=headers, json=payload) as response:
            if response.status != 200:
                raise Exception(f"GPT API 錯誤: {response.status}, {await response.text()}")
            data = await response.json()
            return data['choices'][0]['message']['content']

@app.route('/import_text', methods=['POST'])
async def import_text():
    """處理檔案上傳和知識圖譜構建"""
    try:
        # 檢查是否有上傳檔案
        if 'file' not in (await request.files):
            return jsonify({'status': 'error', 'message': '沒有檔案被上傳'})

        file = (await request.files)['file']
        if file.filename == '':
            return jsonify({'status': 'error', 'message': '未選擇檔案'})

        # # 保存上傳的檔案
        # filename = secure_filename(file.filename)
        # filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        # await file.save(filepath)

        # 獲取安全的檔案名稱
        filename = secure_filename(file.filename)

        # 上傳文件內容到 S3
        s3_url = await asyncio.to_thread(
            lambda: upload_to_s3_public(file.stream, bucket_name, filename)
        )
        if not s3_url:
            return jsonify({'status': 'error', 'message': '檔案備份到 S3 失敗'}), 500

        # 從 S3 讀取文件內容
        content = await asyncio.to_thread(read_from_s3_public, s3_url)
        if not content:
            return jsonify({'status': 'error', 'message': '從 S3 讀取文件內容失敗'}), 500

        # 提示：檔案已成功上傳並讀取內容
        print("檔案已成功上傳並讀取，開始處理文本內容...")

        # 處理從 S3 讀取的文件內容
        lines = content.splitlines()  # 按行分割內容
        filtered_lines = [
            line.strip() for line in lines
            if line.strip() and not line.strip().startswith('```')  # 過濾空行和多餘的 Markdown 標記
        ]
        original_content = "\n".join(filtered_lines)  # 合併為單個字符串

        # 確認處理後的內容
        print("處理後的內容：")
        print(original_content)


        # 調用 GPT API 分解節點、關係及屬性
        gpt_prompt = f"""你是一個專門負責從文本資料中提取結構化知識的助手，目的是生成可以用於 Neo4j 知識圖譜的三元組資料結構。

                        請將以下文本資料轉換成結構化的節點與關係，並滿足以下要求：

                        1. **以三元組為核心設計**：
                        - 每個知識由三元組組成：`節點A - 關係 - 節點B`。
                        - 節點與關係之間的語義必須清晰，能夠直觀表達原始文本的知識。

                        2. **節點的要求**：
                        - 每個節點至少有一個標籤（Label），例如："Person"、"Company" 等。
                        - 節點必須包含屬性，並確保有一個名為 `name` 的屬性作為主要標識。
                        - 如果文本中提供了額外的節點信息，請將其轉換為節點的其他屬性（例如：`age`、`location`、`title` 等）。

                        3. **關係的要求**：
                        - 每個關係必須有一個類型（Type），例如："WORKS_FOR"、"LOCATED_IN" 等。
                        - 關係必須包含屬性，並確保有一個名為 `name` 的屬性作為關係的描述。
                        - 如果文本中提供了額外的關係信息，請將其轉換為關係的其他屬性。

                        4. **輸出格式**：
                        - 節點和關係應該被組織為 JSON 格式，分為 "nodes" 和 "relationships" 兩部分。
                        - 範例結構：
                            {{
                                "nodes": [
                                    {{
                                        "label": ["Label1", "Label2"],
                                        "properties": {{
                                            "name": "節點名稱",
                                            "additional_property1": "屬性值",
                                            "additional_property2": "屬性值"
                                        }}
                                    }}
                                ],
                                "relationships": [
                                    {{
                                        "from": "節點A的name值",
                                        "to": "節點B的name值",
                                        "type": "關係類型",
                                        "properties": {{
                                            "name": "關係名稱",
                                            "additional_property1": "屬性值",
                                            "additional_property2": "屬性值"
                                        }}
                                    }}
                                ]
                            }}
                        5.請確保輸出內容只有"第4點"輸出格式的內容，禁止添加任何註解、標點符號或 Markdown 格式（例如 ` ```json`）。 
                        6.輸出前，請再次確認輸出內容是否符合JSON格式，並且確保沒有多餘內容!
                        請根據以下文本資料進行提取並生成結果：
                        {original_content}
                        """
        
        print("正在調用 GPT 分解文本內容...")
        parsed_data = await call_gpt_api(gpt_prompt)
        # 過濾非法符號，例如去除 Markdown 標記
        parsed_data = parsed_data.strip().replace('```json', '').replace('```', '')
        # 將數據按行分割，移除所有空行，然後重新組合為完整字符串
        parsed_data = "\n".join([line.strip() for line in parsed_data.splitlines() if line.strip()])
        print("文本內容解析成功，生成處理後的檔案...")
        print(parsed_data)
        # 保存解析後的內容為新檔案
        parsed_filepath = os.path.join("processed", f'parsed_{filename}')
        os.makedirs(os.path.dirname(parsed_filepath), exist_ok=True)
        with open(parsed_filepath, 'w', encoding='utf-8') as f:
            f.write(parsed_data)

        # 確保 parsed_data 是字典
        if isinstance(parsed_data, str):

            # 修復可能的格式問題
            parsed_data = parsed_data.strip()  # 去掉首尾空格            
            """
            嘗試修復 JSON 格式，例如未加雙引號的屬性名。
            """
            import re
            # 添加雙引號到未加引號的屬性名
            parsed_data = re.sub(r'(?<!")(\b[a-zA-Z_]+\b)(?=\s*:)', r'"\1"', parsed_data)
            # 移除多餘的逗號，例如 `[1,2,]` 或 `{key: value,}`
            parsed_data = re.sub(r',\s*(\]|\})', r'\1', parsed_data)

            # 修復未正確結束的 JSON，如開頭或結尾的不完整部分
            if not parsed_data.startswith("{"):
                parsed_data = "{" + parsed_data
            if not parsed_data.endswith("}"):
                parsed_data = parsed_data + "}"

            try:
                # 嘗試將修復後的資料解析為 JSON
                parsed_data = json.loads(parsed_data)
            except json.JSONDecodeError as e:
                # 如果解析失敗，打印修復後的數據並報錯
                print("修復後的 JSON 數據如下：")
                print(parsed_data)
                raise ValueError(f"解析 JSON 數據失敗：{e}")

        # 確保 JSON 格式修復完成後進行結構檢查
        if not isinstance(parsed_data, dict):
            raise ValueError("解析後的數據不是字典，請檢查解析結果。")
        if "nodes" not in parsed_data or "relationships" not in parsed_data:
            raise ValueError("解析數據缺少 'nodes' 或 'relationships' 鍵。")

        print("JSON 格式修復完成，正在審核數據結構...")

        # 初始化容器以保存不完整的數據
        invalid_nodes = []
        invalid_relationships = []

        # 審核 nodes
        valid_nodes = []
        for node in parsed_data["nodes"]:
            if isinstance(node, dict) and "label" in node and "properties" in node and "name" in node["properties"]:
                valid_nodes.append(node)
            else:
                invalid_nodes.append(node)

        # 審核 relationships
        valid_relationships = []
        for rel in parsed_data["relationships"]:
            if isinstance(rel, dict) and "from" in rel and "to" in rel and "type" in rel:
                valid_relationships.append(rel)
            else:
                invalid_relationships.append(rel)

        # 打印不完整的數據
        if invalid_nodes:
            print(f"發現 {len(invalid_nodes)} 條不完整的節點數據：")
            for node in invalid_nodes:
                print(node)

        if invalid_relationships:
            print(f"發現 {len(invalid_relationships)} 條不完整的關係數據：")
            for rel in invalid_relationships:
                print(rel)

        # 構建有效的數據結構
        validated_data = {
            "nodes": valid_nodes,
            "relationships": valid_relationships
        }

        print("數據結構審核完成，開始生成 Cypher 語法...")
        
        # 靜態生成節點語法
        cypher_statements = []
        for node in validated_data["nodes"]:
            label = ":".join([f"`{l}`" for l in node["label"]])  # 合併標籤
            properties = ", ".join([f"{k}: '{v}'" for k, v in node["properties"].items()])
            cypher_statements.append(f"CREATE (n:{label} {{{properties}}});")

        # 靜態生成關係語法
        for rel in validated_data["relationships"]:
            from_node = rel["from"]
            to_node = rel["to"]
            rel_type = f"`{rel['type']}`"  # 關係類型
            rel_properties = ", ".join([f"{k}: '{v}'" for k, v in rel["properties"].items()])
            cypher_statements.append(
                f"MATCH (a {{name: '{from_node}'}}), (b {{name: '{to_node}'}}) "
                f"CREATE (a)-[:{rel_type} {{{rel_properties}}}]->(b);"
            )

        # 合併所有語句
        cypher_commands = "\n".join(cypher_statements)

        # 校驗輸出語句是否包含非空內容
        if not cypher_commands.strip():
            raise ValueError("生成的 Cypher 語句為空，請檢查解析的 JSON 數據或 GPT 輸出。")

        # 保存過濾後的 Cypher 檔案
        cypher_filepath = os.path.join("processed", f'cypher_{filename}')
        os.makedirs(os.path.dirname(cypher_filepath), exist_ok=True)
        with open(cypher_filepath, 'w', encoding='utf-8') as f:
            f.write(cypher_commands)

        print("Cypher 命令已生成並過濾，準備導入 Neo4j 資料庫...")

        # 執行 Cypher 命令逐條導入到 Neo4j
        with open(cypher_filepath, 'r', encoding='utf-8') as f:
            cypher_content = f.read().strip()
            # 分割語句並逐條執行
            statements = cypher_content.split(';')
            for statement in statements:
                if statement.strip():  # 確保語句非空
                    print(f"執行語句：{statement.strip()}")
                    graph.run(statement.strip())

        print("資料成功導入 Neo4j，完成知識圖譜構建！")

        return jsonify({'status': 'success', 'message': ''})

    except Exception as e:
        # 捕獲所有異常並返回錯誤訊息
        return jsonify({'status': 'error', 'message': f'處理過程中出現錯誤: {str(e)}'})
    
@app.route('/')
async def index():
    # 渲染主頁面 HTML
    return await render_template('index.html')

#<--- 完整內容 --->
# 刪除所有資料
@app.route('/delete_all_kg', methods=['DELETE'])
async def delete_all_kg():
    try:
        query = "MATCH (n) DETACH DELETE n"
        graph.run(query)
        return jsonify({'status': 'success', 'message': '所有資料已成功刪除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

# 獲取整個知識圖譜的節點和邊資料
@app.route('/get_graph', methods=['GET'])
async def get_graph():
    query = """
        MATCH (n)
        OPTIONAL MATCH (n)-[r]->(m)
        RETURN n, r, m
    """
    results = graph.run(query)

    nodes = {}
    edges = []
    # 构建节点和边的数据
    for record in results:
        n = record['n']
        if n.identity not in nodes:
            nodes[n.identity] = {
                'id': n.identity,
                'label': n.get('name', f'Unnamed ({n.identity})'),
                'shape': 'dot'
            }

        if record['m']:
            m = record['m']
            if m.identity not in nodes:
                nodes[m.identity] = {
                    'id': m.identity,
                    'label': m.get('name', f'Unnamed ({m.identity})'),
                    'shape': 'dot'
                }

            # Ensure `r` exists and has a unique ID
            r = record['r']
            if r:
                edges.append({
                    'id': r.identity,  # Unique relationship ID
                    'from': n.identity,
                    'to': m.identity,
                    'label': r.get('name', ''),  # Show relationship name if it exists
                    'arrows': 'to'
                })

    return jsonify({'nodes': list(nodes.values()), 'edges': edges})

# 更新節點的資料
@app.route('/update_node', methods=['POST'])
async def update_node():
    data = await request.get_json()
    node_id = data.get('node_id')
    node_properties = data.get('node_properties')

    if node_id and node_properties:
        query = """
            MATCH (n) WHERE id(n) = $node_id
            SET n += $node_properties
        """
        graph.run(query, node_id=node_id, node_properties=node_properties)
        return jsonify({'status': 'success', 'updated_properties': node_properties})
    return jsonify({'status': 'error', 'message': 'Missing node_id or properties'})

@app.route('/update_node_label', methods=['POST'])
async def update_node_label():
    data = await request.get_json()
    node_id = data.get('node_id')
    new_label = data.get('label')

    if not node_id or not new_label:
        return jsonify({'status': 'error', 'message': 'Missing node_id or label'})

    try:
        # 獲取標籤資料
        current_labels_query = """
            MATCH (n) WHERE id(n) = $node_id
            RETURN labels(n) AS current_labels
        """
        current_labels = graph.run(current_labels_query, node_id=node_id).data()
        if not current_labels:
            return jsonify({'status': 'error', 'message': 'Node not found'})

        current_labels = current_labels[0]['current_labels']

        # 檢查標籤合法性:中文；英文、數字
        if not all(c.isalnum() or '\u4e00' <= c <= '\u9fff' or c == '_' for c in new_label):
            return jsonify({'status': 'error', 'message': 'Invalid label format. Only alphanumeric, Chinese characters, and underscores are allowed.'})

        # 更新為新標籤
        if current_labels:
            remove_label_query = f"""
                MATCH (n) WHERE id(n) = $node_id
                REMOVE n:{current_labels[0]}
            """
            graph.run(remove_label_query, node_id=node_id)

        add_label_query = f"""
            MATCH (n) WHERE id(n) = $node_id
            SET n:{new_label}
        """
        graph.run(add_label_query, node_id=node_id)
        return jsonify({'status': 'success', 'updated_label': new_label})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Update failed: {str(e)}'})

# 更新邊（關係）的資料
@app.route('/update_edge', methods=['POST'])
async def update_edge():
    data = await request.get_json()
    relationship_id = data.get('relationship_id')
    relationship_name = data.get('relationship_name')

    if relationship_id is None or not relationship_name:
        return jsonify({'status': 'error', 'message': 'Missing relationship_id or relationship_name'})

    try:
        query = """
            MATCH ()-[r]->()
            WHERE id(r) = $relationship_id
            SET r.name = $relationship_name
        """
        graph.run(query, relationship_id=int(relationship_id), relationship_name=relationship_name)
        return jsonify({'status': 'success', 'updated_relationship': relationship_name})
    except Exception as e:
        print(f"Error updating relationship: {e}")  # Log error
        return jsonify({'status': 'error', 'message': f'Failed to update relationship properties: {str(e)}'})

# 更新關係（邊）的屬性
@app.route('/update_edge_properties', methods=['POST'])
async def update_edge_properties():
    """
    更新關係的屬性，不影響標籤。
    """
    data = await request.get_json()
    relationship_id = data.get('relationship_id')
    relationship_properties = data.get('relationship_properties')

    if relationship_id is None or not relationship_properties:
        return jsonify({'status': 'error', 'message': 'Missing relationship_id or properties'})

    try:
        query = """
            MATCH ()-[r]->()
            WHERE id(r) = $relationship_id
            SET r += $relationship_properties
        """
        graph.run(query, relationship_id=int(relationship_id), relationship_properties=relationship_properties)
        return jsonify({'status': 'success', 'updated_properties': relationship_properties})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Failed to update relationship properties: {str(e)}'})

# 新增節點
@app.route('/add_node', methods=['POST'])
async def add_node():
    data = await request.get_json()
    properties = data.get('properties', {})
    
    # 預設屬性包含 "name"
    if "name" not in properties:
        properties["name"] = "未命名節點"
    
    try:
        query = "CREATE (n $properties) RETURN id(n) as node_id"
        result = graph.run(query, properties=properties).data()
        return jsonify({'status': 'success', 'node_id': result[0]['node_id']})
    except ClientError as e:
        return jsonify({'status': 'error', 'message': f'Error: {str(e)}'})

# 新增節點和關係的 API，支援多種新增類型
@app.route('/add_node_with_relationship', methods=['POST'])
async def add_node_with_relationship():
    data = await request.get_json()
    add_type = data.get("add_type")
    tx = graph.begin()  # 開啟 Transaction

    try:
        if add_type == "singleNode":
            result = await add_single_node(tx, data)
        elif add_type == "existingRelation":
            result = await add_existing_relation(tx, data)
        elif add_type == "newNodeRelationExisting":
            result = await add_new_node_relation_existing(tx, data)
        elif add_type == "existingNodeRelationNew":
            result = await add_existing_node_relation_new(tx, data)
        elif add_type == "newNodeRelationNew":
            result = await add_new_node_relation_new(tx, data)
        else:
            return jsonify({"status": "error", "message": "Invalid add type"})

        tx.commit()  # 成功時提交 Transaction
        return result
    except Exception as e:
        tx.rollback()  # 出錯時回滾 Transaction
        return jsonify({"status": "error", "message": f"Transaction failed: {str(e)}"})

# 新增單一新節點
async def add_single_node(tx, data):
    labels = data.get("labels", [])
    properties = data.get("properties", {})

    # 確保有 name 屬性
    if "name" not in properties:
        return jsonify({"status": "error", "message": "name 屬性是必須的"})

    try:
        query = f"CREATE (n:{':'.join(labels)} $properties) RETURN id(n) as node_id, n.name as name"
        result = tx.run(query, properties=properties).data()
        node_id = result[0]['node_id']
        name = result[0]['name']
        return jsonify({'status': 'success', 'node_id': node_id, 'name': name})
    except ClientError as e:
        return jsonify({"status": "error", "message": f"Error: {str(e)}"})

# 新增新關係（已存在的節點）
async def add_existing_relation(tx, data):
    head_id = data.get("head_id")
    tail_id = data.get("tail_id")
    relation_label = data.get("relation_label")  # 單一關係標籤
    properties = data.get("properties", {})

    # 驗證必要參數
    if not head_id or not tail_id:
        return jsonify({"status": "error", "message": "頭節點或尾節點的 ID 是必須的"})

    try:
        # 確保標籤與屬性分離
        query = f"""
        MATCH (h), (t)
        WHERE id(h) = $head_id AND id(t) = $tail_id
        CREATE (h)-[r:{relation_label}]->(t)
        SET r += $properties
        RETURN id(r) as relationship_id, type(r) as type, properties(r) as properties
        """
        result = tx.run(query, head_id=int(head_id), tail_id=int(tail_id), properties=properties).data()

        if not result:
            return jsonify({"status": "error", "message": "新增關係失敗，請檢查資料"})

        relationship_id = result[0]['relationship_id']
        relationship_type = result[0]['type']
        relationship_properties = result[0]['properties']
        return jsonify({
            'status': 'success',
            'relationship_id': relationship_id,
            'type': relationship_type,
            'properties': relationship_properties
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error: {str(e)}"})

# 新增新節點與新關係到現有節點
async def add_new_node_relation_existing(tx, data):
    existing_node_id = data.get("existing_node_id")
    new_node_labels = data.get("new_node_labels", [])
    new_node_properties = data.get("new_node_properties", {})
    relation_labels = data.get("relation_labels", [])
    relation_properties = data.get("relation_properties", {})

    query_new_node = f"CREATE (n:{':'.join(new_node_labels)} $properties) RETURN id(n) as node_id"
    new_node_result = tx.run(query_new_node, properties=new_node_properties).data()
    new_node_id = new_node_result[0]["node_id"]

    query_relationship = f"""
        MATCH (n), (e)
        WHERE id(n) = $new_node_id AND id(e) = $existing_node_id
        CREATE (n)-[r:{':'.join(relation_labels)} $properties]->(e)
        RETURN id(r) as relationship_id
    """
    relationship_result = tx.run(query_relationship, new_node_id=new_node_id, existing_node_id=int(existing_node_id), properties=relation_properties).data()

    return jsonify({'status': 'success', 'new_node_id': new_node_id, 'relationship_id': relationship_result[0]['relationship_id']})

# 新增現有節點與新關係到新節點
async def add_existing_node_relation_new(tx, data):
    existing_node_id = data.get("existing_node_id")
    new_node_labels = data.get("new_node_labels", [])
    new_node_properties = data.get("new_node_properties", {})
    relation_labels = data.get("relation_labels", [])
    relation_properties = data.get("relation_properties", {})

    query_new_node = f"CREATE (n:{':'.join(new_node_labels)} $properties) RETURN id(n) as node_id"
    new_node_result = tx.run(query_new_node, properties=new_node_properties).data()
    new_node_id = new_node_result[0]["node_id"]

    query_relationship = f"""
        MATCH (e), (n)
        WHERE id(e) = $existing_node_id AND id(n) = $new_node_id
        CREATE (e)-[r:{':'.join(relation_labels)} $properties]->(n)
        RETURN id(r) as relationship_id
    """
    relationship_result = tx.run(query_relationship, existing_node_id=int(existing_node_id), new_node_id=new_node_id, properties=relation_properties).data()

    return jsonify({'status': 'success', 'new_node_id': new_node_id, 'relationship_id': relationship_result[0]['relationship_id']})

# 新增新節點與新關係到新節點
async def add_new_node_relation_new(tx, data):
    head_labels = data.get("head_labels", [])
    head_properties = data.get("head_properties", {})
    tail_labels = data.get("tail_labels", [])
    tail_properties = data.get("tail_properties", {})
    relation_labels = data.get("relation_labels", [])
    relation_properties = data.get("relation_properties", {})

    try:
        query_head_node = f"CREATE (h:{':'.join(head_labels)} $head_properties) RETURN id(h) as node_id"
        head_node_result = graph.run(query_head_node, head_properties=head_properties).data()
        head_node_id = head_node_result[0]["node_id"]

        query_tail_node = f"CREATE (t:{':'.join(tail_labels)} $tail_properties) RETURN id(t) as node_id"
        tail_node_result = graph.run(query_tail_node, tail_properties=tail_properties).data()
        tail_node_id = tail_node_result[0]["node_id"]

        query_relationship = f"""
        MATCH (h), (t)
        WHERE id(h) = $head_node_id AND id(t) = $tail_node_id
        CREATE (h)-[r:{':'.join(relation_labels)} $relation_properties]->(t)
        RETURN id(r) as relationship_id
        """
        graph.run(query_relationship, head_node_id=head_node_id, tail_node_id=tail_node_id, relation_properties=relation_properties)

        return jsonify({'status': 'success', 'head_node_id': head_node_id, 'tail_node_id': tail_node_id})
    except ClientError as e:
        return jsonify({"status": "error", "message": f"Error: {str(e)}"})

# @app.route('/delete_edge', methods=['POST'])
# async def delete_edge():
#     data = await request.get_json()
#     relationship_id = data.get('relationship_id')

#     if not relationship_id:
#         return jsonify({'status': 'error', 'message': '缺少 relationship_id'})

#     try:
#         # 根據單一 ID 刪除邊
#         query = """
#             MATCH ()-[r]->()
#             WHERE id(r) = $relationship_id
#             DELETE r
#         """
#         graph.run(query, relationship_id=int(relationship_id))
#         return jsonify({'status': 'success', 'message': f'關係 {relationship_id} 已刪除'})
#     except Exception as e:
#         return jsonify({'status': 'error', 'message': f'刪除失敗: {str(e)}'})

@app.route('/delete_item', methods=['POST'])
async def delete_item():
    data = await request.get_json()
    item_id = data.get('id')
    item_type = data.get('type')  # 'node' or 'relationship'

    if not item_id or not item_type:
        return jsonify({'status': 'error', 'message': '缺少 ID 或類型'})

    try:
        item_id = int(item_id)  # 確保 ID 是整數

        if item_type == 'node':
            # 刪除節點
            node_query = "MATCH (n) WHERE id(n) = $item_id RETURN COUNT(n) AS count"
            node_result = graph.run(node_query, item_id=item_id).data()

            if node_result and node_result[0]['count'] > 0:
                delete_query = "MATCH (n) WHERE id(n) = $item_id DETACH DELETE n"
                graph.run(delete_query, item_id=item_id)
                return jsonify({'status': 'success', 'message': f'節點 {item_id} 已刪除', 'item_type': 'node'})
            else:
                return jsonify({'status': 'error', 'message': f'ID {item_id} 不存在於節點中'})

        elif item_type == 'relationship':
            # 刪除關係
            edge_query = "MATCH ()-[r]->() WHERE id(r) = $item_id RETURN COUNT(r) AS count"
            edge_result = graph.run(edge_query, item_id=item_id).data()

            if edge_result and edge_result[0]['count'] > 0:
                delete_query = "MATCH ()-[r]->() WHERE id(r) = $item_id DELETE r"
                graph.run(delete_query, item_id=item_id)
                return jsonify({'status': 'success', 'message': f'關係 {item_id} 已刪除', 'item_type': 'relationship'})
            else:
                return jsonify({'status': 'error', 'message': f'ID {item_id} 不存在於關係中'})

        else:
            return jsonify({'status': 'error', 'message': '無效的類型'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'刪除過程出錯: {str(e)}'})

@app.route('/delete_node_property', methods=['POST'])
async def delete_node_property():
    data = await request.get_json()
    node_id = data.get('node_id')
    property_key = data.get('property_key')

    if not node_id or not property_key:
        return jsonify({'status': 'error', 'message': '缺少節點 ID 或屬性名稱'})

    try:
        # 動態構建查詢語句
        query = f"""
            MATCH (n) WHERE id(n) = $node_id
            REMOVE n.`{property_key}`
        """
        graph.run(query, node_id=node_id, property_key=property_key)
        return jsonify({'status': 'success', 'message': f'屬性 "{property_key}" 已刪除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'刪除屬性失敗: {str(e)}'})

@app.route('/delete_edge_property', methods=['POST'])
async def delete_edge_property():
    data = await request.get_json()
    edge_id = data.get('edge_id')
    property_key = data.get('property_key')

    if not edge_id or not property_key:
        return jsonify({'status': 'error', 'message': '缺少 edge_id 或 property_key'})

    try:

        query = f"""
            MATCH ()-[r]->() WHERE id(r) = $edge_id
            REMOVE r.`{property_key}`
        """
        graph.run(query, edge_id=int(edge_id), property_key=property_key)
        return jsonify({'status': 'success', 'message': f'屬性 "{property_key}" 已刪除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'刪除屬性失敗: {str(e)}'})

@app.route('/delete_node', methods=['POST'])
async def delete_node():
    data = await request.get_json()
    node_id = data.get('node_id')

    if not node_id:
        return jsonify({'status': 'error', 'message': '缺少節點 ID'})

    try:
        query = """
            MATCH (n) WHERE id(n) = $node_id
            DETACH DELETE n
        """
        graph.run(query, node_id=node_id)
        return jsonify({'status': 'success', 'message': f'節點 {node_id} 已刪除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'刪除節點失敗: {str(e)}'})

@app.route('/delete_edge', methods=['POST'])
async def delete_edge():
    data = await request.get_json()
    relationship_id = data.get('relationship_id')

    if relationship_id is None:
        return jsonify({'status': 'error', 'message': '缺少關係 ID'})

    try:
        query = """
            MATCH ()-[r]->() WHERE id(r) = $relationship_id
            DELETE r
        """
        graph.run(query, relationship_id=relationship_id)
        return jsonify({'status': 'success', 'message': f'邊 {relationship_id} 已刪除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'刪除邊失敗: {str(e)}'})

@app.route('/delete_node_label', methods=['POST'])
async def delete_node_label():
    """
    刪除節點標籤資料 (type(r))。
    """
    data = await request.get_json()
    node_id = data.get('node_id')  # 節點 ID
    label_name = data.get('label')  # 標籤名稱（要刪除的 type(r)）

    if not node_id or not label_name:
        return jsonify({'status': 'error', 'message': '缺少 node_id 或 label_name'})

    try:
        # 刪除標籤資料的查詢
        query = f"""
            MATCH (n)-[r]->() 
            WHERE id(n) = $node_id AND type(r) = $label_name
            DELETE r
        """
        graph.run(query, node_id=int(node_id), label_name=label_name)
        return jsonify({'status': 'success', 'message': f'標籤 {label_name} 已刪除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'刪除標籤失敗: {str(e)}'})

# 查找節點資訊
@app.route('/query_node', methods=['POST'])
async def query_node():
    data = await request.get_json()
    node_id = data.get('node_id')

    # 查找節點及其屬性
    node_query = "MATCH (n) WHERE id(n) = $node_id RETURN n"
    node_result = graph.run(node_query, node_id=node_id).data()
    
    # 查找節點的標籤
    label_query = "MATCH (n) WHERE id(n) = $node_id RETURN DISTINCT labels(n) AS NodeLabels"
    label_result = graph.run(label_query, node_id=node_id).data()
    
    if node_result:
        node = node_result[0]['n']
        properties = {key: node[key] for key in node.keys()}
        labels = label_result[0]['NodeLabels'] if label_result else []

        return jsonify({'status': 'success', 'node': {'id': node.identity, 'properties': properties, 'labels': labels}})
    else:
        return jsonify({'status': 'error', 'message': 'Node not found'})

@app.route('/query_edge', methods=['POST'])
async def query_edge():
    data = await request.get_json()
    relationship_id = data.get('relationship_id')

    if relationship_id is None:
        return jsonify({'status': 'error', 'message': 'Missing relationship_id'})

    try:
        query = """
            MATCH ()-[r]->()
            WHERE id(r) = $relationship_id
            RETURN id(r) AS relationship_id, r
        """
        result = graph.run(query, relationship_id=int(relationship_id)).data()

        if not result:
            return jsonify({'status': 'error', 'message': 'Relationship not found'})

        edge = result[0]['r']
        properties = {key: edge[key] for key in edge.keys()}  # Get relationship properties
        label = edge.get('name', '')  # Default to an empty name if not set

        return jsonify({
            'status': 'success',
            'edge': {
                'id': relationship_id,
                'properties': properties,
                'label': label
            }
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Failed to fetch edge: {str(e)}'})

@app.route('/query_by_relationship_name', methods=['POST'])
async def query_by_relationship_name():
    data = await request.get_json()
    relationship_name = data.get('relationship_name', '').strip()

    if not relationship_name:
        return jsonify({'status': 'error', 'message': '缺少關係名稱'})

    try:
        # 查詢所有符合條件的關係以及相關節點
        query = """
            MATCH (n)-[r]->(m)
            WHERE r.name = $relationship_name
            RETURN n, r, m
        """
        results = graph.run(query, relationship_name=relationship_name).data()

        # 格式化結果
        nodes, edges = format_results(results)
        return jsonify({'status': 'success', 'nodes': nodes, 'edges': edges})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'查詢失敗: {str(e)}'})

@app.route('/query_by_name', methods=['POST'])
async def query_by_name():
    data = await request.get_json()
    query_term = data.get('query_term')

    # 根據節點或關係名稱進行查詢
    query = """
        MATCH (n) WHERE n.name = $query_term
        OPTIONAL MATCH (n)-[r]->(m)
        RETURN n, r, m
    """
    results = graph.run(query, query_term=query_term).data()

    # 將查詢結果格式化
    nodes, edges = format_results(results)
    return jsonify({'status': 'success', 'nodes': nodes, 'edges': edges})

# @app.route('/query_by_sentence', methods=['POST'])
# async def query_by_sentence():
#     data = await request.get_json()
#     sentence = data.get('sentence')

#     # 使用 LLM 剖析句子並生成節點和關係
#     parsed_data = llm_parse_sentence(sentence)  # 假設 llm_parse_sentence 是一個處理函數

#     # 使用剖析結果進行 Cypher 查詢
#     query = """
#         MATCH (n)-[r]->(m)
#         WHERE n.name IN $node_names AND r.name IN $relation_names
#         RETURN n, r, m
#     """
#     node_names = parsed_data['nodes']
#     relation_names = parsed_data['relationships']
#     results = graph.run(query, node_names=node_names, relation_names=relation_names).data()

#     # 將查詢結果格式化
#     nodes, edges = format_results(results)
#     return jsonify({'nodes': nodes, 'edges': edges})

def format_results(results):
    nodes = {}
    edges = []
    for record in results:
        n = record['n']
        if n.identity not in nodes:
            nodes[n.identity] = {
                'id': n.identity,
                'label': n['name'] if 'name' in n else str(n.identity),
                'shape': 'dot'
            }
        if record['m']:
            m = record['m']
            if m.identity not in nodes:
                nodes[m.identity] = {
                    'id': m.identity,
                    'label': m['name'] if 'name' in m else str(m.identity),
                    'shape': 'dot'
                }
            r = record['r']
            if r:
                edges.append({
                    'id': r.identity,
                    'from': n.identity,
                    'to': m.identity,
                    'label': r['name'] if 'name' in r else '',
                    'arrows': 'to'
                })
    return list(nodes.values()), edges

if __name__ == "__main__":
    # 固定彈性 IP
    public_ip = "3.225.199.88"  # 替換為您的彈性 IP 地址(固定的公有IP)

    # 打印應用運行的 URL
    print(f"Running on: http://{public_ip}:5000")
    app.run(host="0.0.0.0", port=5000)
