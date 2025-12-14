import cv2
import socket
import struct
import numpy as np
import time
from collections import deque
import threading

class CameraServer:
    # 定数をクラス変数として定義し、インスタンス生成不要にする
    DATA_TYPE_SIZE = struct.calcsize('>c')
    DATA_LEN_SIZE = struct.calcsize('>L')
    DATA_DECIMAL_SIZE = struct.calcsize('>d')

    def __init__(self, host='0.0.0.0', port=9999, timeout_sec=60):
        self.host = host
        self.port = port
        self.timeout_sec = timeout_sec
        self.latency_history = deque(maxlen=100)
        self.active_clients = [] # 複数のクライアントを管理
        self.lock = threading.Lock()
        
        # 受信したカメラデータとレイテンシ情報を保持する変数
        # 各クライアントからの最新データを保持できるように辞書型に変更
        self.__client_camera_data = {} 
        self.__client_latency_time = {} 

        self.recv_data_running = True # サーバループ全体の実行フラグ

        # 送信するカメラ番号をバイト形式で初期化 (デフォルトはカメラ1)
        self.__send_camera_request = struct.pack('>B', 1) 

        self.qr_detector = cv2.QRCodeDetector()
        self.qr_request = False
        self.qr_result = None # QRコードの表示結果

    def recv_all(self, sock, size):
        data = b''
        while len(data) < size:
            try:
                packet = sock.recv(size - len(data))
                if not packet:
                    # 接続が正常に閉じられた場合
                    return None
                data += packet
            except socket.timeout:
                print(f"[WARN] ソケットタイムアウト ({sock.getpeername()})")
                return None
            except ConnectionResetError:
                print(f"[ERROR] クライアント ({sock.getpeername()}) が強制的に切断されました。")
                return None
            except Exception as e:
                print(f"[ERROR] データ受信エラー ({sock.getpeername()}): {e}")
                return None
        return data
    
    def recv_data(self, sock, addr):
        print(f"[INFO] 受信スレッド開始: {addr}")
        sock.settimeout(self.timeout_sec)
        
        try:
            while self.recv_data_running:
                # 最初にデータタイプ (1バイト) を受信
                data_type_bytes = self.recv_all(sock, CameraServer.DATA_TYPE_SIZE)
                if data_type_bytes is None: # 接続切断など
                    break
                
                data_type = struct.unpack('>c', data_type_bytes)[0]
                
                if data_type == b'c': # カメラデータ
                    # データ長 (4バイト) を受信
                    data_length_bytes = self.recv_all(sock, CameraServer.DATA_LEN_SIZE)
                    if data_length_bytes is None:
                        break
                    data_length = struct.unpack('>L', data_length_bytes)[0]
                    
                    # カメラデータ本体を受信
                    camera_data_bytes = self.recv_all(sock, data_length)
                    if camera_data_bytes is None:
                        break
                    
                    with self.lock:
                        # 複数クライアント対応のため辞書に格納
                        self.__client_camera_data[addr] = camera_data_bytes
                    
                elif data_type == b't': # タイムスタンプデータ
                    # タイムスタンプ (8バイト double) を受信
                    timestamp_bytes = self.recv_all(sock, CameraServer.DATA_DECIMAL_SIZE)
                    if timestamp_bytes is None:
                        break
                    
                    client_timestamp = struct.unpack('>d', timestamp_bytes)[0]
                    server_receive_time = time.time()
                    latency_ms = (server_receive_time - client_timestamp) * 1000
                    
                    with self.lock:
                        self.latency_history.append(latency_ms)
                        # 複数クライアント対応のため辞書に格納
                        self.__client_latency_time[addr] = latency_ms

                else:
                    print(f"[WARN] 予期しないタイプのデータを受信 ({data_type}) from {addr}")
                    # 不明なデータを受信した場合、そのデータが続く可能性があるため、
                    # ある程度のバイト数を読み飛ばすなどして次回の受信に備えるか、
                    # 接続を閉じるかを検討する。今回は一旦接続を閉じないでおく。
                    pass
        
        except Exception as e:
            print(f"[ERROR] クライアント ({addr}) 受信処理中に例外: {e}")
        finally:
            with self.lock:
                if sock in self.active_clients:
                    self.active_clients.remove(sock)
                if addr in self.__client_camera_data:
                    del self.__client_camera_data[addr]
                if addr in self.__client_latency_time:
                    del self.__client_latency_time[addr]

            sock.close()
            print(f"[INFO] クライアント ({addr}) 切断。受信スレッド終了。")
            if not self.active_clients and not self.display_thread_running():
                print("[INFO] 全クライアントが切断されました。サーバを終了します。")
                self.recv_data_running = False # 全クライアント切断でサーバー終了
                cv2.destroyAllWindows()
                # ここでメインスレッドに終了シグナルを送る必要があるが、今回は recv_data_running で制御

    def send_data(self, sock, addr):
        print(f"[INFO] 送信スレッド開始: {addr}")
        try:
            while self.recv_data_running: # サーバループ全体の実行フラグに連動
                with self.lock:
                    # __send_camera_requestは struct.pack('>B', cam_id) されたもの
                    msg = self.__send_camera_request
                
                try:
                    sock.sendall(msg)
                    # print(f"[DEBUG] カメラ要求 {struct.unpack('>B', msg)[0]} を {addr} に送信")
                except BrokenPipeError:
                    print(f"[ERROR] クライアント ({addr}) のパイプが切断されました。")
                    break
                except socket.error as se:
                    print(f"[ERROR] ソケットエラー (送信スレッド {addr}): {se}")
                    break
                
                time.sleep(0.1) # 送信レートの調整
        except Exception as e:
            print(f"[ERROR] クライアント ({addr}) 送信処理中に例外: {e}")
        finally:
            # recv_data側でクライアントをactive_clientsから削除しているので、ここでは不要
            # ただし、例外発生時に確実にクローズされるよう、sock.close()は必要に応じて配置
            pass # 終了処理はrecv_dataで行う

    # クライアントからの最新カメラデータを取得（表示スレッド用）
    def get_latest_camera_data(self):
        with self.lock:
            # 複数クライアントが存在する場合、ここでは最初のクライアントのデータを返す
            # 必要であれば、特定のクライアントのデータを指定できるように引数を追加
            if self.__client_camera_data:
                # 辞書の最初の要素の値を返す
                return next(iter(self.__client_camera_data.values())) 
        return None
    
    # クライアントからの最新レイテンシを取得（表示スレッド用）
    def get_latest_latency_data(self):
        with self.lock:
            if self.latency_history:
                return sum(self.latency_history) / len(self.latency_history)
        return 0.0 # データがない場合は0を返す
    
    # クライアントに送るカメラ番号を設定（メインスレッド/表示スレッドから呼び出し）
    def set_camera_request(self, camera_num):
        if 1 <= camera_num <= 9: # 1から9までの数字に限定
            with self.lock:
                # 1バイトの符号なし整数としてパック
                self.__send_camera_request = struct.pack('>B', camera_num) 
        else:
            print(f"[WARN] 無効なカメラ番号が設定されました: {camera_num}。無視します。")

    def display_thread_running(self):
        # Displayクラスのインスタンスが存在し、表示がアクティブな場合
        # このメソッドはDisplayクラスのインスタンスから呼び出されることを想定
        return True # 今回の修正では、Displayクラスがメインスレッドで動作するため、
                    # ここでは常にTrueを返しても問題ないが、
                    # 将来的にはDisplayクラス内にフラグを持たせ、それを参照するべき

    def run(self): 
        print(f"[INFO] サーバ起動中 ({self.host}:{self.port})") 
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) 
        server_socket.bind((self.host, self.port)) 
        server_socket.listen(5)
        server_socket.settimeout(0.01) # タイムアウトを短くして、より頻繁にメインループが回るようにする

        main_display = Display(server=self) 

        start_time = time.time() 
        timeout_limit = 60 

        # frame_count と display_skip_frames は削除（常に最新を表示するため）
        # frame_count = 0 
        # display_skip_frames = 1 

        while self.recv_data_running:
            try:
                conn, addr = server_socket.accept() 
                with self.lock:
                    self.active_clients.append(conn) 
                print(f"[INFO] 新規クライアント接続: {addr}")
                thread_recv = threading.Thread(target=self.recv_data, args=(conn, addr), daemon=True)
                thread_send = threading.Thread(target=self.send_data, args=(conn, addr), daemon=True)
                thread_recv.start() 
                thread_send.start()
                start_time = time.time() 
            except socket.timeout:
                with self.lock:
                    if not self.active_clients:
                        if time.time() - start_time > timeout_limit:
                            print(f"[INFO] {timeout_limit}秒間誰も接続しなかったためサーバを終了します。")
                            self.recv_data_running = False 
                            break
            except Exception as e:
                print(f"[ERROR] accept中に予期せぬエラー: {e}")
                self.recv_data_running = False 
                break

            # ★ここから変更★
            # クライアントからのデータ表示とキーボード処理 (メインスレッド)
            latest_camera_data = self.get_latest_camera_data()
            avg_latency = self.get_latest_latency_data() # レイテンシも取得

            # 常に最新のデータがあれば表示を試みる
            if latest_camera_data:
                main_display.show_window(latest_camera_data, avg_latency)
            else:
                # データがない場合でもウィンドウを表示し、キー入力は受け付ける
                # show_window内で黒画面表示とwaitKey処理を集約させるため、ここも変更
                main_display.show_window(None, avg_latency) # データがないことを明示的に伝える

            # キーボード処理はDisplayクラスのメソッドに任せるが、waitKeyはDisplayクラス内の一箇所に集約
            main_display.keyboard_processing(self) 
            # ★ここまで変更★

            if not self.recv_data_running: 
                break

        server_socket.close()
        cv2.destroyAllWindows()
        print("[INFO] サーバを終了しました。")

class Display: # クラス名を'display'から'Display'に変更 (PEP8準拠)
    def __init__(self, server):
        self.__server = server # Serverインスタンスへの参照を保持
        self.__window_data = None
        self.qr_request = False
        self.qr_result = None  # QRコードの表示結果
        self.qr_detector = cv2.QRCodeDetector()
        cv2.namedWindow("Camera Feed", cv2.WINDOW_AUTOSIZE) # ウィンドウを作成しておく

    # def clean(): # 未使用かつ実装がないため削除
    #     pass

    def draw_image(self):
        if self.__window_data is not None:
            self.__window_data = cv2.resize(self.__window_data, (640, 480)) # 適切なサイズに調整
        else:
            # データがない場合は黒い画面を表示
            self.__window_data = np.zeros((480, 640, 3), dtype=np.uint8)

    def read_qr(self):
        if self.qr_request and self.__window_data is not None:
            data, points, _ = self.qr_detector.detectAndDecode(self.__window_data)
            if data:
                self.qr_result = f"QR: {data}"
                # 検出されたQRコードの周りにポリゴンを描画
                if points is not None:
                    points = np.intp(points) # 整数型に変換
                    cv2.polylines(self.__window_data, [points], True, (0, 255, 0), 2)
            else:
                self.qr_result = "QR not detected"
            self.qr_request = False

    def draw_text(self, avg_latency):
        if self.__window_data is not None:
            if self.qr_result:
                cv2.putText(self.__window_data, self.qr_result, (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2, cv2.LINE_AA)
            # cv2.putText(self.__window_data, f"Avg Latency: {avg_latency:.1f} ms", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2, cv2.LINE_AA)

    # show_windowの引数を変更し、生のカメラデータとレイテンシを直接受け取る
    def show_window(self, camera_data_bytes, avg_latency):
        if camera_data_bytes is not None:
            self.__window_data = cv2.imdecode(np.frombuffer(camera_data_bytes, np.uint8), cv2.IMREAD_COLOR)
            
            if self.__window_data is None: # デコード失敗
                # デコードに失敗した場合も黒い画面を表示
                self.__window_data = np.zeros((480, 640, 3), dtype=np.uint8)
                print("[WARN] Received data could not be decoded to an image.")

            # リサイズはここで行うか、クライアント側で固定サイズに設定済みなら不要
            # if self.__window_data.shape[1] != 640 or self.__window_data.shape[0] != 480:
            #     self.__window_data = cv2.resize(self.__window_data, (640, 480)) # 不要であればコメントアウト

            self.read_qr() # QRコード検出 (self.__window_dataにテキストや図形が追加される)
            self.draw_text(avg_latency) # テキスト描画 (self.__window_dataにテキストが追加される)
            
        else: # camera_data_bytes が None の場合（まだデータが来ていない、またはエラー）
            self.__window_data = np.zeros((480, 640, 3), dtype=np.uint8) # 黒い画面を表示
            self.draw_text(avg_latency) # レイテンシだけ表示

        cv2.imshow("Camera Feed", self.__window_data)
        # cv2.waitKey(1) は keyboard_processing に移動させるので、ここから削除
        # key = cv2.waitKey(1) & 0xFF # この行を削除


    def keyboard_processing(self, server_instance): 
        # ★ここに cv2.waitKey(1) を集約する★
        key = cv2.waitKey(1) & 0xFF # 描画更新とキー入力をここでまとめて処理

        if key == ord('q'):
            print("[INFO] 'q'が押されました。終了要求。")
            server_instance.recv_data_running = False 
            
        elif ord('1') <= key <= ord('9'):
            requested_cam = key - ord('0')
            server_instance.set_camera_request(requested_cam) 
            self.qr_result = None  
            print(f"[INFO] カメラ {requested_cam} を要求しました。")
            
        elif key == ord('r') or key == ord('R'):
            self.qr_request = True
            print("[INFO] QRコード検出をリクエストしました。")

if __name__ == '__main__':
    server = CameraServer()
    server.run()