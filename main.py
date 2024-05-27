from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_socketio import SocketIO, emit
import threading
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import config
from ssi_fc_data import fc_md_client, model
from ssi_fc_data.fc_md_stream import MarketDataStream
from ssi_fc_data.fc_md_client import MarketDataClient
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask import abort

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db' 
db = SQLAlchemy(app)
socketio = SocketIO(app)


login_manager = LoginManager()
login_manager.init_app(app)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_locked = db.Column(db.Boolean, default=False)

transactions = {}

# Function to get user by username
def get_user(username):
    return User.query.filter_by(username=username).first()

# Function to get user by id
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Other backend code (symbols, market data, etc.) goes here...
client = fc_md_client.MarketDataClient(config)
symbols = []

def clean_securities_data(securities_data):
    cleaned_data = []
    for security in securities_data.get('data', []):
        symbol = security.get('Symbol')
        stock_name = security.get('StockName')
        if symbol and stock_name:
            cleaned_data.append({'Symbol': symbol, 'StockName': stock_name})
    return cleaned_data

def md_get_securities_list():
    req = model.securities('HOSE', 1, 100)
    securities_data = client.securities(config, req)
    cleaned_data = clean_securities_data(securities_data)
    for security in cleaned_data[:25]:
        symbols.append(security)

def get_market_data(message):
    socketio.emit('market_data', message)

def getError(error):
    print(error)

def start_market_data_stream():
    md_get_securities_list()
    threads = []
    for symbol in symbols:
        selected_channel = f'X:{symbol["Symbol"]}'
        mm = MarketDataStream(config, MarketDataClient(config))
        thread = threading.Thread(target=mm.start, args=(get_market_data, getError, selected_channel))
        thread.start()
        threads.append(thread)
    try:
        while True:
            message = input(">> ")
            if message == "exit()":
                break
            for thread in threads:
                mm = thread._target(*thread._args)
                mm.switch_channel(message)
    except KeyboardInterrupt:
        print("Exiting...")
        for thread in threads:
            thread.join()

md_get_securities_list()

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        # Check if username or email already exists
        if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first():
            return 'Username or email already exists!'
        # Create new user
        new_user = User(username=username, email=email, password=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        # Check if user exists and password is correct
        if user:
            if user.is_locked:
                return 'Your account is locked!'
            if check_password_hash(user.password, password):
                login_user(user)
                return redirect(url_for('market_data'))
        return 'Invalid username or password!'
    return render_template('login.html')


@app.route('/')
def home():
    return render_template('base.html')

@app.route('/market_data')
@login_required
def market_data():
    return render_template('market.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/symbols')
@login_required
def get_symbols():
    return jsonify(symbols)

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    users_data = User.query.all()
    return render_template('admin_panel.html', users=users_data)

@app.route('/lock_user/<int:user_id>')
@login_required
@admin_required
def lock_user(user_id):
    user = User.query.get(user_id)
    if user:
        user.is_locked = True
        db.session.commit()
        return redirect(url_for('admin_panel'))
    else:
        abort(404)  # User not found


@app.route('/unlock_user/<int:user_id>')
@login_required
@admin_required
def unlock_user(user_id):
    user = User.query.get(user_id)
    if user:
        user.is_locked = False
        db.session.commit()
        return redirect(url_for('admin_panel'))
    else:
        abort(404)  # User not found


@app.route('/portfolio')
@login_required
def portfolio():
    user_transactions = transactions.get(current_user.id, [])
    symbol_quantities = {}
    for transaction in user_transactions:
        symbol = transaction['symbol']
        quantity = transaction['quantity']
        trade_type = transaction['trade_type']
        if symbol not in symbol_quantities:
            symbol_quantities[symbol] = 0
        if trade_type == 'buy':
            symbol_quantities[symbol] += quantity
        elif trade_type == 'sell':
            symbol_quantities[symbol] -= quantity
    error_message = request.args.get('error')
    return render_template('portfolio.html', transactions=user_transactions, symbol_quantities=symbol_quantities, error_message=error_message)

@app.route('/trade', methods=['POST'])
@login_required
def trade():
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        quantity = int(data.get('quantity'))
        trade_type = data.get('trade_type')

        # Check if the symbol is in the symbols list
        if not any(s['Symbol'] == symbol for s in symbols):
            return jsonify(success=False, message='Invalid symbol! The symbol does not exist in the market.'), 400

        # Find the symbol in the symbols list
        symbol_index = next((index for index, s in enumerate(symbols) if s['Symbol'] == symbol), None)
        if symbol_index is None:
            return jsonify(success=False, message='Invalid symbol! The symbol does not exist in the market.'), 400

        # Ensure that the symbol dictionary has the 'Quantity' key
        if 'Quantity' not in symbols[symbol_index]:
            symbols[symbol_index]['Quantity'] = 0

        if current_user.id not in transactions:
            transactions[current_user.id] = []

        # Record the transaction
        transactions[current_user.id].append({
            'symbol': symbol,
            'quantity': quantity,
            'trade_type': trade_type,
            'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

        # Update the quantity based on trade type
        if trade_type == 'buy':
            symbols[symbol_index]['Quantity'] += quantity
        elif trade_type == 'sell':
            if symbols[symbol_index]['Quantity'] < quantity:
                return jsonify(success=False, message='Not enough stocks to sell!'), 400
            symbols[symbol_index]['Quantity'] -= quantity

        return jsonify(success=True), 200

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500

# Import necessary libraries
import random

all_symbols = [symbol['Symbol'] for symbol in symbols]
def suggest_stocks_to_buy(all_symbols):
    # Randomly select a symbol from the list of all symbols
    suggested_symbol = random.choice(all_symbols)
    return suggested_symbol

@app.route('/suggest_stock_to_buy')
@login_required
def suggest_stock_to_buy():
    suggested_symbol = suggest_stocks_to_buy(all_symbols)
    return jsonify({'suggested_symbol': suggested_symbol})


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        admin_username = '1'
        admin_password = '1'
        admin_email = 'admin@example.com'

        admin = User.query.filter_by(username=admin_username).first()
        if not admin:
            admin = User(username=admin_username, email=admin_email, password=generate_password_hash(admin_password), is_admin=True)
            db.session.add(admin)
            db.session.commit()
    # Run the Flask app
    market_data_thread = threading.Thread(target=start_market_data_stream)
    market_data_thread.start()
    socketio.run(app, debug=True, port=5001)
